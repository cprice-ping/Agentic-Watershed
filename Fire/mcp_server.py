"""
Fire MCP Server
---------------
Exposes NASA FIRMS satellite hotspot data (collected by collector.py) as
MCP tools that an agent harness can call via the Model Context Protocol.

Tools:
  get_recent_hotspots(n)              Last N hotspot detections
  get_hotspots_since(hours_ago)       Hotspots detected in the last N hours
  get_nearest_hotspots(n)             Closest N hotspots, with FRP standing
                                      and any matching CAL FIRE incident
  get_active_incidents(n)             Nearest named CAL FIRE incidents,
                                      statewide (not limited to the bbox)
  get_hotspot_count_since(hours_ago)  Quick count — is anything nearby at all
  write_agent_observation(...)        Agent writes its own reasoning back to DB

Run (stdio, for Claude Desktop / agent harness):
  python mcp_server.py

Run (HTTP, for testing with MCP Inspector):
  python mcp_server.py --http
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Compact JSON for every tool payload. Pretty-printing spent about 30%
# of a payload on whitespace — 3,800 tokens per River run of pure
# indentation. Shared with the agents' runtime so all four serialise alike.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import compact_json  # noqa: E402

# Resolve flag_rules from this file's directory rather than relying on
# sys.path[0], which is only the script's directory when this module is
# run as a script — it is also imported directly (tests, offline eval).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import flag_rules  # noqa: E402
import thresholds  # noqa: E402
from collector import haversine_mi  # noqa: E402

# ---------------------------------------------------------------------------
# Config — points at the same DB the collector writes to
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "fire.db"

# The hotspots table keeps every row forever — without a matching recency
# window here, a single old detection with nothing closer since would stay
# "nearest" indefinitely and read as an ongoing current signal long after it's
# aged out of what FIRMS would even still report. Defined in thresholds.py so
# flag_rules.py and ATProto/publisher.py apply the identical window; the
# publisher having no window at all is what published a five-day-old hotspot
# as an 8.5-mile threat.
NEAREST_HOTSPOT_MAX_AGE_HOURS = thresholds.NEAREST_HOTSPOT_MAX_AGE_HOURS

mcp = FastMCP(
    "fire",
    instructions=(
        "You have access to NASA FIRMS satellite-detected thermal hotspot data "
        "within a bounding box around Napa Valley. Use these tools to check for "
        "active fire signatures near the region — not fire weather (that's a "
        "separate Weather agent) but actual detected heat sources. Always call "
        "get_nearest_hotspots first to orient yourself."
    ),
)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Model provenance
# ---------------------------------------------------------------------------

def _agent_model() -> str | None:
    """Model id the agent is running, from AGENT_MODEL in the environment.

    Deliberately taken from the environment rather than a tool argument. This
    value ends up in the published record's `agentModel` field, which exists
    so consumers can weight an observation by the capability of whatever
    produced it — a claim the model must not be able to make about itself.
    agent.py sets it before spawning this server; the LLM never sees it.

    None when unset, so the row records "we don't know" instead of a guess.
    """
    return os.environ.get("AGENT_MODEL", "").strip() or None


def _token_usage() -> tuple[int | None, int | None]:
    """Token counts for the call that produced this observation.

    Set by agent.py from response.usage before this server is spawned. Absent
    on a dry run or an older agent, in which case the row records NULL rather
    than a zero that would read as a real measurement.
    """
    def _n(name: str) -> int | None:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw.isdigit() else None
    return _n("AGENT_INPUT_TOKENS"), _n("AGENT_OUTPUT_TOKENS")


def _ensure_shadow_columns(conn: sqlite3.Connection) -> None:
    """Add columns to databases created before they existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_observations)")}
    for name, decl in (("model", "TEXT"),
                       ("rules_flagged", "INTEGER"),
                       ("rules_fired", "TEXT"),
                       ("input_tokens", "INTEGER"),
                       ("output_tokens", "INTEGER")):
        if name not in cols:
            conn.execute(f"ALTER TABLE agent_observations ADD COLUMN {name} {decl}")


def _rule_verdict(conn: sqlite3.Connection) -> tuple[int | None, str | None]:
    """Deterministic flag verdict, recorded alongside the model's own.

    Shadow only — nothing reads this to decide whether to flag. It exists so
    the disagreement between the rules and the model is measurable before
    anyone makes the rules authoritative. Never raises: a broken rule must not
    be able to fail an agent run that would otherwise have written its
    observation.
    """
    try:
        verdict = flag_rules.evaluate(conn)
        return int(verdict.must_flag), verdict.as_json()
    except Exception:
        return None, None


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recent_hotspots(n: int = 20) -> str:
    """
    Return the most recently collected N hotspot detections, regardless of
    distance. Use this to see everything currently known in the bounding box.

    Args:
        n: Number of hotspots to return (default 20, max 200)
    """
    n = min(n, 200)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, latitude, longitude, acq_date, acq_time,
                   satellite, confidence, frp, daynight, distance_mi
            FROM hotspots
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    if not rows:
        return "No hotspots in database yet. Run the collector first."
    return json.dumps(_rows_to_dicts(rows))


@mcp.tool()
def get_hotspots_since(hours_ago: float = 48.0) -> str:
    """
    Return all hotspots collected in the last N hours, nearest first.
    Note this reflects when we polled, not necessarily new satellite passes —
    FIRMS NRT data itself typically updates a few times per day.

    Args:
        hours_ago: How many hours back to look (default 48)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, latitude, longitude, acq_date, acq_time,
                   satellite, confidence, frp, daynight, distance_mi
            FROM hotspots
            WHERE collected_at >= ?
            ORDER BY distance_mi ASC
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No hotspots found in the last {hours_ago} hours."
    return compact_json(_rows_to_dicts(rows))


def _frp_history(conn) -> list[float]:
    """Every FRP reading this collector has ever recorded, ascending."""
    return [r[0] for r in conn.execute(
        "SELECT frp FROM hotspots WHERE frp IS NOT NULL ORDER BY frp ASC"
    ).fetchall()]


def _percentile(sorted_values: list[float], value: float) -> float:
    """Percentage of *sorted_values* at or below *value*."""
    if not sorted_values:
        return 0.0
    import bisect
    return round(100.0 * bisect.bisect_right(sorted_values, value)
                 / len(sorted_values), 1)


def _frp_context(conn) -> dict:
    """The distribution to read a single FRP reading against.

    Without this the agent has a number and nothing to compare it to, and it
    fills the gap by guessing — "well beyond anything previously reported"
    about a reading 3% above the prior peak. A percentile and a count of
    stronger prior detections make "is this unusual" answerable instead of
    rhetorical.

    Bounded by the record, like every other counter here: the answer is
    always relative to what this collector has seen, never to the fire
    history of the region.
    """
    values = _frp_history(conn)
    span = conn.execute(
        "SELECT MIN(acq_date) AS a, MAX(acq_date) AS b FROM hotspots"
    ).fetchone()
    ctx = {
        "detections_with_frp": len(values),
        "record_starts": span["a"] if span else None,
        "record_ends": span["b"] if span else None,
        "basis": ("percentiles are over this collector's own record only, "
                  "not the fire history of the region"),
    }
    if values:
        ctx["max_frp_on_record"] = round(values[-1], 1)
        ctx["median_frp"] = round(values[len(values) // 2], 1)
        notable = thresholds.notable_frp_at(values)
        if notable is not None:
            ctx["notable_at_or_above_frp"] = round(notable, 1)
        else:
            ctx["notable_at_or_above_frp_note"] = (
                f"undefined: fewer than {thresholds.MIN_FRP_HISTORY} FRP "
                f"readings on record, too few to place one in a distribution")
        ctx["notable_percentile"] = thresholds.FRP_NOTABLE_PERCENTILE
    return ctx


def _annotate_frp(hotspots: list[dict], values: list[float]) -> None:
    """Attach each hotspot's standing in the FRP record, in place."""
    for h in hotspots:
        frp = h.get("frp")
        if frp is None:
            continue
        pct = _percentile(values, frp)
        stronger = sum(1 for v in values if v > frp)
        h["frp_percentile"] = pct
        h["frp_stronger_prior_detections"] = stronger
        h["frp_is_notable"] = pct >= thresholds.FRP_NOTABLE_PERCENTILE


def _incidents(conn) -> list[dict]:
    """Known CAL FIRE incidents with usable coordinates, nearest first."""
    try:
        return [dict(r) for r in conn.execute(
            """SELECT name, county, location, latitude, longitude, acres_burned,
                      percent_contained, incident_type, is_active, started_at,
                      updated_at, extinguished_at, url, distance_mi
               FROM incidents WHERE latitude IS NOT NULL AND longitude IS NOT NULL
               ORDER BY distance_mi ASC"""
        ).fetchall()]
    except sqlite3.Error:
        # Table absent until incidents_collector.py has run at least once.
        return []


# A match identifies a detection. An absence identifies nothing, for two
# independent reasons: publication lags ignition, and the feed is a curated
# subset (484 incidents for all of 2026, no prescribed-burn category, and a
# real fire near Willits on 2026-09-09 absent from it entirely). This wording
# is load-bearing: an unmatched hotspot is uncharacterised, never cleared.
_UNMATCHED_NOTE = (
    "No published CAL FIRE incident corresponds to this detection. That does "
    "NOT mean it is not a fire and does NOT mean it is harmless. Publication "
    "lags ignition — a satellite sees the heat before an incident is reported "
    "and published — and the feed is a curated subset rather than a census: "
    "484 incidents statewide for all of 2026, small fires often never listed, "
    "and no prescribed-burn category at all. This detection is uncharacterised."
)


def _parse_dt(value):
    """A CAL FIRE timestamp as an aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _hotspot_detected_at(h: dict):
    """When the satellite saw it: acq_date plus acq_time (HHMM UTC)."""
    dt = _parse_dt(h.get("acq_date"))
    if dt is None:
        return _parse_dt(h.get("collected_at"))
    raw = str(h.get("acq_time") or "").strip().zfill(4)
    if raw.isdigit() and len(raw) == 4:
        dt = dt.replace(hour=int(raw[:2]) % 24, minute=int(raw[2:]) % 60)
    return dt


def _was_burning(inc: dict, when) -> bool:
    """Could this incident have produced a detection at *when*?

    Distance alone is not a match. On 2026-09-09 the five incidents nearest
    Napa were all 100% contained, so a location test by itself would label a
    fresh detection near any of them as a known, closed event.

    Padded at both ends: a satellite sees heat before an incident is reported,
    and ground stays hot after containment. An incident with no usable start
    date is allowed through on distance alone rather than silently dropped —
    it is still a named incident, and the caller shows the dates.
    """
    if when is None:
        return True
    start = _parse_dt(inc.get("started_at"))
    if start is None:
        return True
    if when < start - timedelta(days=thresholds.INCIDENT_MATCH_LEAD_DAYS):
        return False
    if inc.get("is_active"):
        return True
    end = (_parse_dt(inc.get("extinguished_at"))
           or _parse_dt(inc.get("updated_at")))
    if end is None:
        return True
    return when <= end + timedelta(days=thresholds.INCIDENT_MATCH_TAIL_DAYS)


def _match_incidents(hotspots: list[dict], incidents: list[dict]) -> None:
    """Attach the nearest plausible named incident to each hotspot, in place.

    Plausible means near it AND burning when it was detected. Both tests are
    required; distance alone matches a detection today against a fire that
    closed months ago.
    """
    for h in hotspots:
        lat, lon = h.get("latitude"), h.get("longitude")
        detected = _hotspot_detected_at(h)
        best = None
        if lat is not None and lon is not None:
            for inc in incidents:
                if not _was_burning(inc, detected):
                    continue
                try:
                    d = haversine_mi(float(lat), float(lon),
                                     float(inc["latitude"]), float(inc["longitude"]))
                except (TypeError, ValueError):
                    continue
                if d <= thresholds.incident_match_radius_mi(inc["acres_burned"]) and (
                        best is None or d < best[0]):
                    best = (d, inc)
        if best is None:
            h["incident"] = None
            h["incident_note"] = _UNMATCHED_NOTE
        else:
            d, inc = best
            h["incident"] = {
                "name": inc["name"],
                "type": inc["incident_type"],
                "county": inc["county"],
                "acres_burned": inc["acres_burned"],
                "percent_contained": inc["percent_contained"],
                "is_active": bool(inc["is_active"]),
                "started_at": inc["started_at"],
                "miles_from_hotspot": round(d, 1),
                "url": inc["url"],
            }


@mcp.tool()
def get_active_incidents(n: int = 10) -> str:
    """
    Return the nearest known CAL FIRE incidents to Napa Valley, statewide.

    Deliberately not limited to the FIRMS bounding box. On 2026-09-09 the
    nearest actual wildfire was at Willits, 96 miles away and outside the box
    entirely, so the satellite feed could not see it at all. This tool is how
    a fire beyond the monitored area becomes visible.

    Coverage caveat, which matters for how you read an empty or short list:
    an incident appears only once reported and published, so a fire burning
    right now may not be listed yet, and the feed is curated rather than
    complete — 484 incidents statewide for all of 2026. Absence is not
    evidence of nothing burning.

    Args:
        n: Number of nearest incidents to return (default 10)
    """
    with _db() as conn:
        incidents = _incidents(conn)
        stale = conn.execute(
            "SELECT MAX(last_seen_at) AS t FROM incidents"
        ).fetchone() if incidents else None

    if not incidents:
        return compact_json({
            "incidents": [],
            "note": ("No incident data. Either incidents_collector.py has not "
                     "run, or CAL FIRE currently lists no active incidents. "
                     "These are very different — check the collector before "
                     "concluding anything from an empty list."),
        })

    return compact_json({
        "incident_data_last_updated": stale["t"] if stale else None,
        "coverage": ("CAL FIRE published incidents statewide, not limited to "
                     "the FIRMS bounding box. Publication lags ignition, and "
                     "the feed is curated rather than complete — a small fire "
                     "may never be listed. Absence is not evidence of quiet."),
        "nearest_incidents": incidents[:n],
    })


@mcp.tool()
def get_nearest_hotspots(n: int = 10) -> str:
    """
    Return the N *currently relevant* hotspots (detected within the last
    ~day_range+1 days, matching how far back FIRMS itself is queried),
    closest to home_lat/home_lon (Napa Valley center) first. This is the
    primary "is there a fire near us right now" check — call this first.

    A hotspot older than this window is excluded even if nothing closer has
    been detected since — an old single detection with no fresher activity
    means "nothing current nearby," not "this old one is still the nearest
    current threat." Use get_hotspots_since for a longer historical view.

    Call get_last_poll_status separately to check whether the collector
    itself is running and succeeding.

    Args:
        n: Number of nearest hotspots to return (default 10)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEAREST_HOTSPOT_MAX_AGE_HOURS)).isoformat()
    with _db() as conn:
        last_poll = conn.execute(
            "SELECT polled_at, status, error_message FROM polls ORDER BY polled_at DESC LIMIT 1"
        ).fetchone()

        rows = conn.execute(
            """
            SELECT latitude, longitude, acq_date, acq_time, satellite,
                   confidence, frp, daynight, distance_mi, collected_at
            FROM hotspots
            WHERE collected_at >= ?
            ORDER BY distance_mi ASC
            LIMIT ?
            """,
            (cutoff, n),
        ).fetchall()

        frp_values = _frp_history(conn)
        frp_ctx = _frp_context(conn)
        incidents = _incidents(conn)

    result = {
        "last_poll_at": last_poll["polled_at"] if last_poll else None,
        "last_poll_status": last_poll["status"] if last_poll else "never_polled",
        "currency_window_hours": NEAREST_HOTSPOT_MAX_AGE_HOURS,
        "frp_context": frp_ctx,
    }
    if last_poll and last_poll["error_message"]:
        # Set on status="error" (every source failed) and also on a partial
        # failure (status stays "ok" if at least one source succeeded, but
        # error_message notes which source(s) didn't).
        result["last_poll_error"] = last_poll["error_message"]

    if not rows:
        result["nearest_hotspots"] = []
        result["note"] = (
            f"No hotspots detected within {NEAREST_HOTSPOT_MAX_AGE_HOURS}h in the bounding box "
            "— this means nothing current, not that older historical hotspots don't exist."
        )
    else:
        hotspots = _rows_to_dicts(rows)
        _annotate_frp(hotspots, frp_values)
        _match_incidents(hotspots, incidents)
        result["nearest_hotspots"] = hotspots
    return compact_json(result)


@mcp.tool()
def get_last_poll_status() -> str:
    """
    Return the outcome of the most recent collector run: whether it
    succeeded, when it ran, and how many hotspots it fetched/inserted.
    Use this to distinguish "collector is healthy, genuinely no fires" from
    "collector is failing" — a quiet hotspot table can mean either.
    """
    with _db() as conn:
        row = conn.execute(
            """
            SELECT polled_at, status, hotspots_fetched, hotspots_new, error_message
            FROM polls
            ORDER BY polled_at DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return compact_json({"status": "never_polled"})
    return json.dumps(dict(row))


@mcp.tool()
def get_hotspot_count_since(hours_ago: float = 24.0,
                            max_distance_mi: float = thresholds.FAR_DISTANCE_MI) -> str:
    """
    Quick count of hotspots within max_distance_mi in the last N hours —
    useful for a fast "has anything changed" check before pulling full detail.

    Args:
        hours_ago: How many hours back to look (default 24)
        max_distance_mi: Only count hotspots within this distance (default 50mi)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as n, MIN(distance_mi) as closest_mi, MAX(frp) as max_frp
            FROM hotspots
            WHERE collected_at >= ? AND distance_mi <= ?
            """,
            (cutoff, max_distance_mi),
        ).fetchone()
    return compact_json({
        "hotspot_count": row["n"],
        "closest_distance_mi": row["closest_mi"],
        "max_frp_mw": row["max_frp"],
        "window_hours": hours_ago,
        "max_distance_mi": max_distance_mi,
    })


@mcp.tool()
def write_agent_observation(
    summary: str,
    flagged: bool = False,
    reasoning: str = "",
) -> str:
    """
    Write the agent's observation and reasoning back to the database.
    Call this at the end of each agent run to persist conclusions.
    This forms the memory that future agent runs will read.

    Args:
        summary:   A concise human-readable summary of current conditions
                   (1-3 sentences). This is what future runs will see first.
        flagged:   True if conditions warrant attention or follow-up.
        reasoning: The agent's full reasoning, including which data points
                   drove the conclusion. Can be longer.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        _ensure_shadow_columns(conn)
        rules_flagged, rules_fired = _rule_verdict(conn)
        in_tok, out_tok = _token_usage()
        conn.execute(
            """
            INSERT INTO agent_observations
                (observed_at, summary, flagged, reasoning, model,
                 rules_flagged, rules_fired, input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, summary, int(flagged), reasoning, _agent_model(),
             rules_flagged, rules_fired, in_tok, out_tok),
        )
        conn.commit()
    return compact_json({"status": "ok", "observed_at": now})


@mcp.tool()
def get_recent_observations(n: int = 5) -> str:
    """
    Return the agent's most recent written observations.
    Call this at the start of each run for continuity — this is your memory
    of what previous runs concluded.

    Deliberately excludes the full `reasoning` column — summary is what a
    prior run wrote specifically to be read back as memory (see
    write_agent_observation's docstring); reasoning is the audit trail and
    is large enough that echoing it back every run materially inflates
    token cost for no continuity benefit summary doesn't already provide.

    Args:
        n: Number of past observations to retrieve (default 5)
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT observed_at, summary, flagged
            FROM agent_observations
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    if not rows:
        return "No previous observations recorded. This appears to be a fresh run."
    return json.dumps(_rows_to_dicts(rows))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fire MCP Server")
    parser.add_argument("--http", action="store_true", help="Run over HTTP (for Inspector)")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
