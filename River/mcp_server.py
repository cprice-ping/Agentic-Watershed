"""
Watershed MCP Server
--------------------
Exposes Napa River gauge data (collected by collector.py) as MCP tools
that an agent harness can call via the Model Context Protocol.

Tools:
  get_recent_readings(n)              Last N readings across all stations
  get_readings_since(hours_ago)       Every reading from the last N hours
  get_hourly_series(hours_ago)        The same window as hourly min/max — what
                                      the agent actually calls; the raw form
                                      cost ~60k tokens to say the river is low
  get_station_summary(station_id)     Latest values for a single station
  get_anomalies(threshold_pct)        Readings deviating from recent mean
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

# Compact JSON for every tool payload. Pretty-printing spent about 30%
# of a payload on whitespace — 3,800 tokens per River run of pure
# indentation. Shared with the agents' runtime so all four serialise alike.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import compact_json  # noqa: E402

# Resolve River's own modules from this file's directory rather than relying
# on sys.path[0], which is only the script's directory when this module is run
# as a script — it is also imported directly (tests, offline eval).
_sys.path.insert(0, str(Path(__file__).resolve().parent))

# Shared with collector.py — the same routine/condition split decides which
# readings its log marks and which qualifiers these tools spell out.
import qualifiers  # noqa: E402

# The rating floor and the diel cycle. Both are properties of this station
# that a bare number cannot carry, and both produced a wrong published
# record on 2026-09-10.
import hydrology  # noqa: E402

# The deterministic surge verdict, recorded next to the model's own.
import flag_rules  # noqa: E402

# ---------------------------------------------------------------------------
# Config — points at the same DB the collector writes to
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "watershed.db"

mcp = FastMCP(
    "watershed",
    instructions=(
        "You have access to real-time and historical Napa River gauge data "
        "from two USGS monitoring stations. Use these tools to understand "
        "current river conditions, identify anomalies, and record your "
        "observations. Always call get_recent_readings first to orient yourself. "
        "Readings carry a USGS `qualifier` code. Every code present is glossed "
        "once in `qualifier_legend`; a code that says something about the "
        "measurement is ALSO spelled out on its own row in "
        "`qualifier_meaning`, so anything inline is worth reading. Nearly "
        "every reading is 'P' (provisional), which is USGS's review state and "
        "says nothing about the river — it is not a reason to doubt a value. "
        "Ice, Eqp or Bkw do describe the measurement, and a value carrying one "
        "may be wrong in a way its magnitude alone will not reveal."
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


def _qualifier_legend(rows) -> dict:
    """Every qualifier code present in *rows*, glossed once.

    A legend rather than a field on every row. Glossing per row cost about
    15% of River's prompt to restate "P (provisional, subject to revision)"
    768 times, in a database where 28,788 of 28,788 readings are P — the
    gloss was added (2026-09-07) to stop the agent guessing what the letters
    meant, and the cheapest way to do that is to say each one once.
    """
    seen = {}
    for r in rows:
        for code in qualifiers.split(dict(r).get("qualifier")):
            if code not in seen:
                seen[code] = qualifiers.describe(code)
    return seen


def _rows_to_dicts(rows) -> list[dict]:
    """Convert result rows to dicts, annotating only what a legend cannot.

    The qualifier gloss moved to _qualifier_legend, with one exception: a
    NOTABLE code stays on its own row as well. Ice or Eqp appearing once in
    a hundred readings is the case the annotation exists for, and a reader
    scanning rows should not have to cross-reference a legend to notice that
    one measurement is compromised. Routine codes — P, A — are in the legend
    only, because they are on everything and single out nothing.
    """
    out = []
    for r in rows:
        d = dict(r)
        if qualifiers.is_notable(d.get("qualifier")):
            d["qualifier_meaning"] = qualifiers.describe(d.get("qualifier"))
        note = hydrology.floor_note(d.get("parameter_name"), d.get("value"))
        if note:
            d["value_note"] = note
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recent_readings(n: int = 20) -> str:
    """
    Return the most recent N readings across all stations and parameters.
    Use this first to get a sense of current conditions.

    Args:
        n: Number of readings to return (default 20, max 200)
    """
    n = min(n, 200)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, station_name, parameter_name, value, unit, qualifier
            FROM readings
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    if not rows:
        return "No readings in database yet. Run the collector first."
    return json.dumps({"readings": _rows_to_dicts(rows),
                       "qualifier_legend": _qualifier_legend(rows)})


@mcp.tool()
def get_readings_since(hours_ago: float = 24.0) -> str:
    """
    Return all readings from the last N hours.
    Useful for trend analysis and spotting changes over a time window.

    Args:
        hours_ago: How many hours back to look (default 24)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, station_id, station_name,
                   parameter_name, value, unit, qualifier
            FROM readings
            WHERE collected_at >= ?
            ORDER BY station_id, parameter_code, collected_at
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No readings found in the last {hours_ago} hours."
    return compact_json({"readings": _rows_to_dicts(rows),
                       "qualifier_legend": _qualifier_legend(rows)})


def _next_hour(hour_label: str) -> str:
    """The hour bucket one hour after this one, in the query's own format.

    Used to tell a continuous run of hours from one with a hole in it. The
    labels come from `substr(collected_at,1,13) || ':00'`, so they are
    "YYYY-MM-DDTHH:00" and comparing them as text would make 23:00 and the
    next 00:00 look non-adjacent.
    """
    try:
        dt = datetime.strptime(hour_label, "%Y-%m-%dT%H:00")
    except (TypeError, ValueError):
        return ""          # unparseable: never contiguous, so never spanned
    return (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")


@mcp.tool()
def get_hourly_series(hours_ago: float = 48.0) -> str:
    """
    Return the last N hours grouped into one block per station+parameter,
    each holding spans of consecutive hours over which min, max and
    qualifiers did not change. Use this for the shape of recent conditions
    rather than get_readings_since, which returns every reading.

    A span states its own inclusive `from` and `to` and how many hours it
    covers, so no value depends on its position in a list. A collector gap
    ends a span instead of being spanned over — a range always covers every
    hour inside it.

    The collector polls every 15 minutes, so 48 hours of two stations and two
    parameters is 768 rows — about 60,000 tokens, most of River's prompt, to
    convey a trend that 96 hourly rows convey in 4,400. The detail was not
    buying anything: consecutive 15-minute readings at this station differ by
    a hundredth of a foot or not at all.

    Hourly min and max rather than a mean, because the min is the part that
    matters here — the daily low is what crosses the rating floor, and a mean
    would hide it.

    Args:
        hours_ago: How many hours back to summarise (default 48)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT substr(collected_at, 1, 13) || ':00' AS hour,
                   station_id, parameter_name, unit,
                   MIN(value) AS value_min, MAX(value) AS value_max,
                   COUNT(*) AS readings,
                   GROUP_CONCAT(DISTINCT qualifier) AS qualifiers
            FROM readings
            WHERE collected_at >= ? AND value IS NOT NULL
            GROUP BY hour, station_id, parameter_code
            ORDER BY station_id, parameter_code, hour
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No readings found in the last {hours_ago} hours."

    # Grouped into one block per (station, parameter), with consecutive
    # unchanged hours collapsed into a span.
    #
    # The flat form repeated station_id, station_name, parameter_name and
    # unit on every one of ~192 rows: measured on a real 48-hour window,
    # 56% of the payload was key names and repeated dimension values, for
    # data whose non-time columns hold two distinct values each. It was four
    # series with a shared time axis wearing a table costume.
    #
    # Spans rather than parallel arrays, which would have been smaller still.
    # A parallel array detaches every value from its hour and relies on the
    # reader counting positions against a list 48 entries long — and a value
    # arriving without the frame that gives it meaning is the defect this
    # repo keeps rediscovering, most recently a gage height that was really a
    # timestamp. Every span carries its own `from` and `to`, so nothing here
    # depends on position.
    #
    # The compression is proportional to how little happened, which is the
    # right shape: a quiet 48 hours collapses to four spans (97% smaller),
    # while a window containing the 2026-08-31 surge keeps 59 spans (86%)
    # because that detail is the thing worth having.
    legend = {}
    series: dict = {}
    for r in rows:
        d = dict(r)
        codes = [c for c in (d.get("qualifiers") or "").split(",") if c]
        for c in codes:
            legend.setdefault(c, qualifiers.describe(c))

        key = (d["station_id"], d["parameter_name"])
        block = series.get(key)
        if block is None:
            block = {
                "station_id": d["station_id"],
                "parameter_name": d["parameter_name"],
                "unit": d["unit"],
                "spans": [],
            }
            series[key] = block

        at_floor = hydrology.at_floor(d.get("parameter_name"), d.get("value_min"))
        # Qualifiers are aggregated per hour, so a rare code stays visible
        # even though the individual readings are gone. Glossed once per span
        # when notable, as the row-level tools do per row.
        note = (qualifiers.describe(",".join(codes))
                if any(qualifiers.is_notable(c) for c in codes) else None)
        shape = (d["value_min"], d["value_max"], d.get("qualifiers"), note, at_floor)

        prev = block["spans"][-1] if block["spans"] else None
        # A span may only extend across CONSECUTIVE hours. A collector gap
        # must break it: "from 02:00 to 09:00" over a window missing four of
        # those hours would assert coverage that does not exist, which is the
        # same failure as a baseline built from readings that were not
        # measurements.
        contiguous = prev is not None and _next_hour(prev["to"]) == d["hour"]
        if prev is not None and contiguous and prev["_shape"] == shape:
            prev["to"] = d["hour"]
            prev["hours"] += 1
            continue

        span = {"from": d["hour"], "to": d["hour"], "hours": 1,
                "min": d["value_min"], "max": d["value_max"],
                "readings": d["readings"], "_shape": shape}
        if d.get("qualifiers"):
            span["qualifiers"] = d["qualifiers"]
        if note:
            span["qualifier_note"] = note
        # A flag, not a sentence. The floor note is ~140 characters and the
        # hourly minimum is at the floor for most discharge spans; spelling it
        # out per span rebuilds the repetition the legend removed. Explained
        # once below.
        if at_floor:
            span["min_at_rating_floor"] = True
        block["spans"].append(span)

    blocks = []
    for block in series.values():
        for s in block["spans"]:
            s.pop("_shape", None)
            # Collapsed away when a span is a single hour, since from and to
            # already say so.
            if s["hours"] == 1:
                del s["to"]
        blocks.append(block)

    result = {
        "series": blocks,
        "qualifier_legend": legend,
        "note": ("One block per station+parameter. Each span is a run of "
                 "CONSECUTIVE hours over which min, max and qualifiers did "
                 "not change; `from`/`to` are inclusive and `hours` counts "
                 "them, so a span always covers every hour in its range. A "
                 "single-hour span has no `to`. A gap in collection ends a "
                 "span rather than being spanned over. Call "
                 "get_readings_since only if individual readings matter; for "
                 "a trend they do not."),
    }
    if any(s.get("min_at_rating_floor") for b in blocks for s in b["spans"]):
        result["min_at_rating_floor_means"] = hydrology.floor_note(
            "Streamflow", hydrology.DISCHARGE_FLOOR_CFS)
    return compact_json(result)


@mcp.tool()
def get_station_summary(station_id: str = "11458000") -> str:
    """
    Return the latest reading for each parameter at a single station,
    plus a 7-day min/mean/max for context.

    Each latest reading carries a `daily_cycle` block giving the last 24
    hours' min and max, where this reading sits between them, and the reading
    from the same point in yesterday's cycle. Use that last figure for any
    day-over-day statement: this station cycles once daily, so two readings
    taken at different times of day differ because of the hour, not because
    the river changed.

    A discharge reading of 0.0 carries a `value_note` saying it is at the
    rating curve's floor. That is not a measurement of zero flow and does not
    mean the channel is dry.

    Args:
        station_id: USGS station ID. Known stations:
                    11458000 = Napa River near Napa (default)
                    11456000 = Napa River near St Helena
    """
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db() as conn:
        # Latest value per parameter
        latest = conn.execute(
            """
            SELECT parameter_name, value, unit, qualifier, collected_at, usgs_datetime
            FROM readings
            WHERE station_id = ?
            GROUP BY parameter_code
            HAVING collected_at = MAX(collected_at)
            """,
            (station_id,),
        ).fetchall()

        # 7-day stats per parameter
        stats = conn.execute(
            """
            SELECT parameter_name, unit,
                   COUNT(*) as n_readings,
                   MIN(value) as min_val,
                   AVG(value) as mean_val,
                   MAX(value) as max_val
            FROM readings
            WHERE station_id = ? AND collected_at >= ? AND value IS NOT NULL
            GROUP BY parameter_code
            """,
            (station_id, cutoff_7d),
        ).fetchall()

    if not latest:
        return f"No data found for station {station_id}."

    # Place each latest reading inside the last 24 hours of its own
    # parameter. Without this the agent compared each run against its own
    # previous observation — and since runs are 12 hours apart while this
    # station cycles once a day, every comparison straddled opposite phases.
    # On 2026-09-10 an afternoon trough was compared against the previous
    # midnight's peak and published as "flow crashed... in current reading",
    # when the series was in fact rising.
    with _db() as conn:
        day_rows = conn.execute(
            """
            SELECT collected_at, parameter_name, value
            FROM readings
            WHERE station_id = ? AND collected_at >= ? AND value IS NOT NULL
            """,
            (station_id, (datetime.now(timezone.utc)
                          - timedelta(hours=hydrology.DIEL_PERIOD_HOURS * 1.2)
                          ).isoformat()),
        ).fetchall()

    latest_out = _rows_to_dicts(latest)
    for d in latest_out:
        frame = hydrology.diel_frame(day_rows, d.get("parameter_name"),
                                     d.get("value"), d.get("collected_at"))
        if frame:
            d["daily_cycle"] = frame

    result = {
        "station_id": station_id,
        "latest": latest_out,
        "seven_day_stats": _rows_to_dicts(stats),
        "reading_this_station": (
            "Stage here follows a once-daily cycle: highest before dawn, "
            "lowest in the afternoon, with a range of roughly 0.1 ft. That "
            "pattern is measured, not inferred. Its cause is NOT established "
            "— the single daily peak and the small amplitude rule out tide, "
            "but nothing here distinguishes evapotranspiration from an "
            "irrigation withdrawal schedule or anything else, so do not name "
            "a cause. Use daily_cycle.same_phase_24h_ago for any day-over-day "
            "claim; comparing against the previous agent run compares "
            "different times of day, not different days."
        ),
    }
    return compact_json(result)


@mcp.tool()
def get_anomalies(threshold_pct: float = 50.0, lookback_days: int = 30) -> str:
    """
    Find recent readings that deviate significantly from the rolling mean.
    Returns readings where the value differs from the mean by more than
    threshold_pct percent.

    Args:
        threshold_pct: Percentage deviation to flag as anomalous (default 50)
        lookback_days: How many days of history to compute the baseline mean from
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with _db() as conn:
        # The baseline is built from measurable readings only — see
        # hydrology.MIN_MEASURABLE_SHARE_FOR_BASELINE. Averaging the whole
        # window, floor included, produced a "normal" made largely of
        # non-measurements, and one that sank a little further every day the
        # river stayed at the floor.
        #
        # Every row in the window is pulled rather than aggregated in SQL,
        # because which rows are at the floor is a question about the
        # parameter name and the value together, and that lives in
        # hydrology.at_floor rather than in a WHERE clause.
        window = conn.execute(
            """
            SELECT station_id, parameter_code, parameter_name, value, unit
            FROM readings
            WHERE collected_at >= ? AND value IS NOT NULL
            """,
            (cutoff,),
        ).fetchall()

        # Get last 24h readings
        recent = conn.execute(
            """
            SELECT station_id, parameter_code, parameter_name,
                   value, unit, collected_at, qualifier
            FROM readings
            WHERE collected_at >= ? AND value IS NOT NULL
            ORDER BY collected_at DESC
            """,
            (recent_cutoff,),
        ).fetchall()

    # Build the baseline lookup: mean of measurable readings, plus what
    # share of the window that mean rests on. A key is absent entirely when
    # too little of the window was measurable — absent means "no usable
    # normal", which is a different claim from a small number.
    totals: dict = {}
    for r in window:
        key = (r["station_id"], r["parameter_code"])
        seen, kept, total = totals.get(key, (0, 0, 0.0))
        at_floor = hydrology.at_floor(r["parameter_name"], r["value"])
        totals[key] = (seen + 1,
                       kept + (0 if at_floor else 1),
                       total + (0.0 if at_floor else r["value"]))

    baseline_map: dict = {}
    baseline_basis: dict = {}
    for key, (seen, kept, total) in totals.items():
        share = kept / seen if seen else 0.0
        baseline_basis[key] = {
            "readings_in_window": seen,
            "measurable_readings": kept,
            "measurable_share_pct": round(100 * share, 1),
        }
        if kept and share >= hydrology.MIN_MEASURABLE_SHARE_FOR_BASELINE:
            baseline_map[key] = total / kept

    anomalies = []
    floor_pinned = []
    unbaselined: list = []
    for row in recent:
        key = (row["station_id"], row["parameter_code"])

        # Checked before the baseline is looked up, not after. A floor
        # reading needs no baseline to be reported, and testing the baseline
        # first made this branch unreachable in the one situation it exists
        # for: a river pinned at its floor long enough that no baseline is
        # computable is exactly a river whose floor readings must still be
        # reported.
        if hydrology.at_floor(row["parameter_name"], row["value"]):
            d = dict(row)
            # No baseline_mean here, deliberately. Withholding the percentage
            # while supplying both operands is not withholding it: on
            # 2026-09-14 this row carried value 0.0 and baseline_mean 0.826
            # next to a note saying no percentage would be given, and the
            # published summary read "100% below 30-day baseline". If the
            # ratio is meaningless, so is the pair of numbers that makes it.
            d["value_note"] = hydrology.floor_note(row["parameter_name"],
                                                   row["value"])
            d["deviation_note"] = (
                "No deviation percentage and no baseline are given for this "
                "reading: it sits at the rating floor, so any comparison "
                "against a baseline would describe the gauge rather than the "
                "river. Do not derive one.")
            floor_pinned.append(d)
            continue

        mean = baseline_map.get(key)
        if mean is None or mean == 0:
            continue
        # A reading at the rating floor is reported, never scored. Dividing
        # by a 30-day mean of 0.826 cfs gave "100% deviation" for a 0.0 that
        # is the instrument's floor, and that number reached a published
        # record as "severe drought conditions emerging".
        deviation_pct = abs(row["value"] - mean) / abs(mean) * 100
        if deviation_pct >= threshold_pct:
            d = dict(row)
            d["baseline_mean"] = round(mean, 3)
            d["baseline_basis"] = baseline_basis.get(key)
            d["deviation_pct"] = round(deviation_pct, 1)
            anomalies.append(d)

    # Which parameters have no usable baseline at all. Built from the window
    # rather than from the rows that happened to be scored, because "there is
    # no 30-day normal for discharge here" is a fact about the record and
    # stays true whether or not the current reading is at the floor. Saying it
    # outright is the point: the floor rows already carry "do not derive one",
    # and this week has been a lesson in how weak an instruction to refrain is
    # next to simply not providing the thing.
    for key in dict.fromkeys((r["station_id"], r["parameter_code"]) for r in recent):
        if key in baseline_map:
            continue
        basis = baseline_basis.get(key)
        if not basis:
            continue
        name = next((r["parameter_name"] for r in recent
                     if (r["station_id"], r["parameter_code"]) == key), None)
        unbaselined.append({
            "station_id": key[0],
            "parameter_code": key[1],
            "parameter_name": name,
            **basis,
            "why": (
                f"Fewer than "
                f"{hydrology.MIN_MEASURABLE_SHARE_FOR_BASELINE:.0%} of the "
                f"{lookback_days}-day window was above the rating floor, so no "
                f"baseline is computed and none should be assumed. A mean over "
                f"mostly floor readings describes how long the gauge has been "
                f"unmeasurable rather than what is normal here, and it sinks "
                f"further every day the floor persists — which would make a "
                f"deepening dry spell read as a milder anomaly."),
        })

    anomalies.sort(key=lambda x: x["deviation_pct"], reverse=True)
    result = {"anomalies": anomalies}
    if floor_pinned:
        result["at_rating_floor"] = floor_pinned
    if unbaselined:
        result["no_baseline"] = unbaselined
    result["baseline_note"] = (
        f"Baselines are the mean of MEASURABLE readings over "
        f"{lookback_days} days — readings at the rating floor are excluded "
        f"from them, and baseline_basis says how much of the window each "
        f"mean rests on. Where too little was measurable, the parameter is "
        f"listed under no_baseline with no mean at all rather than given a "
        f"thin one.")
    if not anomalies:
        result["note"] = (
            f"No scored anomalies (>{threshold_pct}% deviation) in the last "
            f"24 hours."
            + (" Readings at the rating floor are listed separately and are "
               "deliberately unscored." if floor_pinned else ""))
    return compact_json(result)


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
    parser = argparse.ArgumentParser(description="Watershed MCP Server")
    parser.add_argument("--http", action="store_true", help="Run over HTTP (for Inspector)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
