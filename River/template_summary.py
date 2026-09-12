"""
Template summariser — River
---------------------------
The control arm. Produces a watershed summary from the same database the
agent reads, with no model involved, so the two can be compared on identical
inputs.

The question it exists to answer: after a week of moving site knowledge into
code — the rating floor, the diel cycle, the same-phase comparison, the
qualifier legend — is there anything left in a River observation that needs a
model? The agent costs roughly 35,000 input tokens twice a day to conclude
the river is low. This costs nothing. If its summaries are as good, that is
an answer; if they are visibly worse, the difference is what the model adds.

Deliberately a pure function of (conn, observed_at). Nothing here reads the
agent's prior prose, so it can be run retroactively over every observation
already recorded rather than waiting a fortnight to accumulate a sample.

On flagging, the honest position: River has no flag_rules.py because it has
no numeric flag criteria. `floodStageThresholdFt` is not configured for
either Napa gauge, so there is no threshold a rule could evaluate. This
template therefore flags only on data quality — a collector that has stopped
— and says so rather than inventing a condition. Any conditions flag the
agent raises is a judgement with no arithmetic behind it, which the
comparison should make visible rather than hide.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import hydrology
import qualifiers

# A collector gap this long means the summary describes stale water. The
# collector polls every 15 minutes, so three hours is twelve missed polls —
# well past noise, and short enough to catch the 2026-09-10 outage.
STALE_AFTER_HOURS = 3.0

# Window for the min/mean/max context line, matching get_station_summary.
STATS_WINDOW_DAYS = 7


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(value, unit: str) -> str:
    if value is None:
        return "no reading"
    return f"{value:g} {unit}"


def _station_lines(conn, station_id: str, station_name: str,
                   observed_at: str) -> tuple[list[str], list[str]]:
    """One station's sentences, plus any data-quality notes."""
    obs_dt = _parse(observed_at)
    lines, notes = [], []

    latest = conn.execute(
        """
        SELECT parameter_name, value, unit, collected_at, qualifier
        FROM readings
        WHERE station_id = ? AND collected_at <= ?
        GROUP BY parameter_code
        HAVING collected_at = MAX(collected_at)
        """,
        (station_id, observed_at),
    ).fetchall()
    if not latest:
        return [f"{station_name}: no readings on record at this time."], notes

    day_rows = conn.execute(
        """
        SELECT collected_at, parameter_name, value
        FROM readings
        WHERE station_id = ? AND value IS NOT NULL AND collected_at <= ?
          AND collected_at >= ?
        """,
        (station_id, observed_at,
         (obs_dt - timedelta(hours=hydrology.DIEL_PERIOD_HOURS * 1.2)).isoformat()
         if obs_dt else observed_at),
    ).fetchall()

    parts = []
    for r in latest:
        name, value, unit = r["parameter_name"], r["value"], r["unit"]
        piece = f"{name.split(',')[0].strip()} {_fmt(value, unit)}"

        # The floor is stated, never converted into a percentage or a verdict.
        if hydrology.at_floor(name, value):
            piece += " (at or below the gauge's measurable minimum, not a dry channel)"

        frame = hydrology.diel_frame(day_rows, name, value, r["collected_at"])
        pos = frame.get("position_in_daily_range_pct")
        if pos is not None:
            piece += f", {pos}% of today's range"
        same = frame.get("same_phase_24h_ago")
        if same is not None:
            delta = hydrology.percent_change(value, same, name)
            piece += (f", {same:g} at the same hour yesterday"
                      + (f" ({delta:+.1f}%)" if delta is not None else ""))
        elif frame:
            notes.append(f"{station_name}: no reading 24h ago to compare "
                         f"{name.split(',')[0].strip().lower()} against.")

        if qualifiers.is_notable(r["qualifier"]):
            notes.append(f"{station_name}: {name.split(',')[0].strip()} "
                         f"qualified {r['qualifier']} — the measurement itself "
                         f"may be affected.")
        parts.append(piece)

        age_h = None
        t = _parse(r["collected_at"])
        if t and obs_dt:
            age_h = (obs_dt - t).total_seconds() / 3600.0
        if age_h is not None and age_h > STALE_AFTER_HOURS:
            notes.append(f"{station_name}: newest {name.split(',')[0].strip().lower()} "
                         f"reading is {age_h:.1f}h old — the collector has gaps.")

    lines.append(f"{station_name}: " + "; ".join(parts) + ".")

    stats = conn.execute(
        """
        SELECT parameter_name, unit, MIN(value) AS lo, AVG(value) AS mean,
               MAX(value) AS hi
        FROM readings
        WHERE station_id = ? AND value IS NOT NULL
          AND collected_at <= ? AND collected_at >= ?
        GROUP BY parameter_code
        """,
        (station_id, observed_at,
         (obs_dt - timedelta(days=STATS_WINDOW_DAYS)).isoformat()
         if obs_dt else observed_at),
    ).fetchall()
    if stats:
        bits = [f"{s['parameter_name'].split(',')[0].strip()} "
                f"{s['lo']:g}–{s['hi']:g} (mean {s['mean']:.2f}) {s['unit']}"
                for s in stats]
        lines.append(f"{station_name} over {STATS_WINDOW_DAYS} days: "
                     + "; ".join(bits) + ".")
    return lines, notes


def summarise(conn, observed_at: str, stations: dict) -> dict:
    """Build the control summary for one observation time.

    *stations* maps station_id to display name, as node_config.json holds it.

    Returns summary text, a flag, and the basis for the flag — the same three
    things the agent returns, so the comparison is like for like.
    """
    conn.row_factory = sqlite3.Row
    lines, notes = [], []
    for sid, sname in stations.items():
        sl, sn = _station_lines(conn, sid, sname, observed_at)
        lines += sl
        notes += sn

    flags = []
    for n in notes:
        if "collector has gaps" in n:
            flags.append(n)

    # No condition flag is possible here and that is a fact about the
    # configuration, not an oversight: floodStageThresholdFt is unset for both
    # Napa gauges, so there is no numeric criterion for the river being high,
    # and "low" in September is the season. Stated in the output so a reader
    # comparing against the agent knows the template is not merely silent.
    summary = " ".join(lines)
    if notes:
        summary += " Notes: " + " ".join(notes)
    summary += (" No flood-stage threshold is configured for either gauge, so "
                "no condition flag is computable; this summary flags only on "
                "collector gaps.")

    return {
        "summary": summary,
        "flagged": bool(flags),
        "basis": flags or ["no collector gap; no condition criteria configured"],
    }
