"""
Fire MCP Server
---------------
Exposes NASA FIRMS satellite hotspot data (collected by collector.py) as
MCP tools that an agent harness can call via the Model Context Protocol.

Tools:
  get_recent_hotspots(n)              Last N hotspot detections
  get_hotspots_since(hours_ago)       Hotspots detected in the last N hours
  get_nearest_hotspots(n)             Closest N hotspots to home_lat/home_lon
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — points at the same DB the collector writes to
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "fire.db"

_NODE_CFG_PATH = Path(__file__).parent.parent / "node_config.json"
_DAY_RANGE = json.loads(_NODE_CFG_PATH.read_text())["fire"]["day_range"]

# FIRMS itself is only ever queried for the last day_range days (collector.py),
# but the hotspots table keeps every row forever — without a matching recency
# window here, a single old detection with nothing closer since would stay
# "nearest" indefinitely and read as an ongoing current signal long after it's
# aged out of what FIRMS would even still report.
NEAREST_HOTSPOT_MAX_AGE_HOURS = _DAY_RANGE * 24 + 24  # + 1 day buffer for poll timing

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


def _ensure_model_column(conn: sqlite3.Connection) -> None:
    """Add agent_observations.model to databases created before it existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_observations)")}
    for name, decl in (("model", "TEXT"),
                       ("input_tokens", "INTEGER"),
                       ("output_tokens", "INTEGER")):
        if name not in cols:
            conn.execute(f"ALTER TABLE agent_observations ADD COLUMN {name} {decl}")


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
    return json.dumps(_rows_to_dicts(rows), indent=2)


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
    return json.dumps(_rows_to_dicts(rows), indent=2)


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
                   confidence, frp, daynight, distance_mi
            FROM hotspots
            WHERE collected_at >= ?
            ORDER BY distance_mi ASC
            LIMIT ?
            """,
            (cutoff, n),
        ).fetchall()

    result = {
        "last_poll_at": last_poll["polled_at"] if last_poll else None,
        "last_poll_status": last_poll["status"] if last_poll else "never_polled",
        "currency_window_hours": NEAREST_HOTSPOT_MAX_AGE_HOURS,
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
        result["nearest_hotspots"] = _rows_to_dicts(rows)
    return json.dumps(result, indent=2)


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
        return json.dumps({"status": "never_polled"})
    return json.dumps(dict(row), indent=2)


@mcp.tool()
def get_hotspot_count_since(hours_ago: float = 24.0, max_distance_mi: float = 50.0) -> str:
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
    return json.dumps({
        "hotspot_count": row["n"],
        "closest_distance_mi": row["closest_mi"],
        "max_frp_mw": row["max_frp"],
        "window_hours": hours_ago,
        "max_distance_mi": max_distance_mi,
    }, indent=2)


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
        _ensure_model_column(conn)
        in_tok, out_tok = _token_usage()
        conn.execute(
            """
            INSERT INTO agent_observations
                (observed_at, summary, flagged, reasoning, model,
                 input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, summary, int(flagged), reasoning, _agent_model(),
             in_tok, out_tok),
        )
        conn.commit()
    return json.dumps({"status": "ok", "observed_at": now})


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
    return json.dumps(_rows_to_dicts(rows), indent=2)


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
