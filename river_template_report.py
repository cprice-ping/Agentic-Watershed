"""
Template-vs-agent report — River

Runs River/template_summary.py against every observation the agent has
already written and puts the two side by side. Because the template is a pure
function of (database, observed_at), this does not need a fortnight of
shadow running — the sample already exists.

Two things it reports.

SIDE BY SIDE. The agent's summary and the template's, for the same instant,
from the same rows. That comparison is qualitative and needs a person; this
tool's job is to make it cheap to look at, not to score it.

NUMERIC TRACEABILITY. Every number in a summary is extracted and checked
against values the database can produce at that time — readings, seven-day
statistics, and same-phase deltas. A number that matches nothing is reported
as UNVERIFIED, not as false: the check is a heuristic and will not recognise
a figure the writer computed some other legitimate way. But it is the closest
thing available to an objective measure of the prose, and it is what would
have caught "flow crashed from 0.32 cfs to 0.03 cfs in current reading" on
2026-09-10, where the current reading was 0.03 and rising.

Standalone and offline, like token_report.py and shadow_report.py. Nothing
imports it and no agent depends on it.

Usage:
  python3 river_template_report.py                  # last 14 days
  python3 river_template_report.py --days 60
  python3 river_template_report.py --limit 3 --full # show whole summaries
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "River"))

import template_summary  # noqa: E402
import hydrology         # noqa: E402

DB = BASE / "River" / "data" / "watershed.db"
STATIONS = json.loads((BASE / "node_config.json").read_text())["watershed"]["usgs_stations"]

# Whole numbers are window sizes and counts — "over 7 days", "past 12+ hours",
# "30-day baseline" — not measurements. They go to a separate bucket rather
# than being scored.
#
# NOT a magnitude cutoff. The first version of this dropped everything below
# 3.0, which in a river running at hundredths of a cubic foot per second is
# almost every real value: it silently swallowed the fabricated "0.826 cfs
# baseline" this check exists to catch. Readings here carry decimals; window
# sizes do not, and that is the distinction that holds.
#
# Domain-specific, and only claimed for River. A domain whose measurements
# are whole numbers — AQI, hotspot counts — would need a different rule.
def _is_window_size(token: str, follows: str) -> bool:
    if "." in token or abs(float(token)) >= 1000:
        return False
    # An integer followed by a percent sign is a claim about a change, not a
    # window: "100% deviation from a 30-day baseline" contains one of each,
    # and only the second is furniture.
    return not follows.lstrip().startswith("%")

# How close a quoted figure must be to a database value to count as matched.
# Generous, because summaries round: 0.295 quoted against a mean of 0.2951.
TOLERANCE = 0.02

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def supported_values(conn, observed_at: str) -> set[float]:
    """Every number the database can legitimately produce at this instant.

    Readings in the last week, their seven-day min/mean/max, the daily
    min/max and same-phase values from the diel frame, and the percentage
    deltas those imply. Deliberately generous — the aim is to surface figures
    with no possible source, not to police rounding.
    """
    obs = template_summary._parse(observed_at)
    if obs is None:
        return set()
    vals: set[float] = set()
    week = (obs - timedelta(days=template_summary.STATS_WINDOW_DAYS)).isoformat()

    rows = conn.execute(
        """SELECT collected_at, station_id, parameter_name, value FROM readings
           WHERE value IS NOT NULL AND collected_at <= ? AND collected_at >= ?""",
        (observed_at, week),
    ).fetchall()
    for r in rows:
        vals.add(round(float(r["value"]), 3))

    for sid in STATIONS:
        stats = conn.execute(
            """SELECT MIN(value) lo, AVG(value) mean, MAX(value) hi
               FROM readings WHERE station_id = ? AND value IS NOT NULL
                 AND collected_at <= ? AND collected_at >= ?
               GROUP BY parameter_code""",
            (sid, observed_at, week),
        ).fetchall()
        for s in stats:
            for v in (s["lo"], s["mean"], s["hi"]):
                if v is not None:
                    vals.add(round(float(v), 3))

        day = [r for r in rows if r["station_id"] == sid]
        names = {r["parameter_name"] for r in day}
        for name in names:
            cur = [r for r in day if r["parameter_name"] == name]
            if not cur:
                continue
            newest = max(cur, key=lambda r: r["collected_at"])
            frame = hydrology.diel_frame(day, name, newest["value"],
                                         newest["collected_at"])
            for key in ("min", "max", "same_phase_24h_ago",
                        "position_in_daily_range_pct", "same_phase_change_pct"):
                if frame.get(key) is not None:
                    vals.add(round(float(frame[key]), 3))
            # The delta between the newest reading and yesterday's same phase,
            # which a writer may legitimately quote either way round.
            same = frame.get("same_phase_24h_ago")
            if same:
                d = hydrology.percent_change(newest["value"], same, name)
                if d is not None:
                    vals.add(round(d, 1))
                    vals.add(round(abs(d), 1))
    return vals


def unverified(text: str, vals: set[float]) -> tuple[list[str], list[str]]:
    """Numbers in *text* with no match in *vals*, split from trivial ones."""
    missing, trivial = [], []
    text = text or ""
    for m in _NUM.finditer(text):
        tok = m.group(0)
        follows = text[m.end():m.end() + 2]
        try:
            n = float(tok)
        except ValueError:
            continue
        # Prose furniture, not claims about water: years, clock times, and
        # the station identifiers the summaries name explicitly.
        if 1900 <= n <= 2100 or ("." not in tok and len(tok) == 4):
            continue
        if tok in STATIONS:
            continue
        if any(abs(n - v) <= TOLERANCE or
               (v and abs(n - v) / max(abs(v), 1e-9) <= 0.01) for v in vals):
            continue
        (trivial if _is_window_size(tok, follows) else missing).append(tok)
    return missing, trivial


def main() -> None:
    ap = argparse.ArgumentParser(description="River template vs agent")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=0, help="show at most N runs")
    ap.add_argument("--full", action="store_true", help="print whole summaries")
    args = ap.parse_args()

    if not DB.exists():
        print(f"No database at {DB}")
        return
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    try:
        runs = conn.execute(
            """SELECT observed_at, summary, flagged, model, status
               FROM agent_observations WHERE observed_at >= ?
               ORDER BY observed_at DESC""", (since,)).fetchall()
    except sqlite3.OperationalError:
        runs = conn.execute(
            """SELECT observed_at, summary, flagged, model
               FROM agent_observations WHERE observed_at >= ?
               ORDER BY observed_at DESC""", (since,)).fetchall()

    print(f"River: agent vs template, last {args.days} day(s)")
    print("The template is a control. Nothing here was published.\n")

    shown = 0
    agent_unver = tmpl_unver = 0
    agent_flags = tmpl_flags = 0
    compared = 0
    for r in runs:
        try:
            if (r["status"] or "").lower() == "failed":
                continue
        except (IndexError, KeyError):
            pass
        t = template_summary.summarise(conn, r["observed_at"], STATIONS)
        vals = supported_values(conn, r["observed_at"])
        a_missing, _ = unverified(r["summary"], vals)
        t_missing, _ = unverified(t["summary"], vals)
        compared += 1
        agent_unver += len(a_missing)
        tmpl_unver += len(t_missing)
        agent_flags += 1 if r["flagged"] else 0
        tmpl_flags += 1 if t["flagged"] else 0

        if args.limit and shown >= args.limit:
            continue
        shown += 1
        print(f"--- {r['observed_at'][:19]}  agent flagged="
              f"{bool(r['flagged'])}  template flagged={t['flagged']}")
        cut = None if args.full else 400
        print(f"  AGENT    : {(r['summary'] or '')[:cut]}")
        if a_missing:
            print(f"  UNVERIFIED in agent summary: {', '.join(a_missing)}")
        print(f"  TEMPLATE : {t['summary'][:cut]}")
        if t_missing:
            print(f"  UNVERIFIED in template summary: {', '.join(t_missing)}")
        print()

    if not compared:
        print("No agent observations in this window.")
        return
    print(f"=== {compared} run(s) compared")
    print(f"  flagged      agent {agent_flags}, template {tmpl_flags}")
    print(f"  unverified   agent {agent_unver} figure(s), "
          f"template {tmpl_unver}")
    print("\n  UNVERIFIED means no value in the database matched, within 1% or "
          "\n  0.02 absolute. It is a heuristic: a legitimately derived figure "
          "\n  the checker does not know how to reproduce also lands here.")
    if tmpl_flags == 0 and agent_flags:
        print("\n  The template never flags on conditions by construction — "
              "\n  floodStageThresholdFt is unconfigured, so no numeric "
              "criterion\n  exists. Every agent condition-flag is judgement "
              "with no arithmetic\n  behind it; whether that is worth paying "
              "for is the question.")


if __name__ == "__main__":
    main()
