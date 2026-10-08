"""
Deterministic flag rules — AQI
------------------------------
Evaluates agent.py's AQI flag criteria in code. All four are arithmetic over
`observations.aqi` and `observations.category_number`.

Currently SHADOW ONLY — recorded alongside the model's own flagged value,
never overriding it.

Two of the rules are comparisons against an earlier reading rather than a
threshold on the current one, so they need the recent series, not just the
latest row.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_runtime import NOTE_PREFIX  # noqa: E402

# Every threshold comes from thresholds.py, which agent.py's prompt is also
# generated from — one definition, so the prompt and these rules cannot
# disagree about what counts as a flag.
from thresholds import (  # noqa: E402
    PM25,
    PM25_UNHEALTHY_SENSITIVE, PM25_RISE_POINTS, PM25_RISE_WINDOW_HOURS,
    PM25_JUMP_TO, PM25_JUMP_FROM, CATEGORY_UNHEALTHY, SERIES_WINDOW_HOURS,
    COLLECTOR_STALE_AFTER_HOURS,
)


class Verdict:
    """Outcome of evaluating the rules, on two channels, as in Fire.

    `fired` forces a flag. `notes` records something measured and worth
    keeping that is not by itself a reason to alarm — see
    agent_runtime.NOTE_PREFIX.
    """

    def __init__(self) -> None:
        self.fired: list[str] = []
        self.notes: list[str] = []

    @property
    def must_flag(self) -> bool:
        return bool(self.fired)

    def fire(self, rule: str, detail: str) -> None:
        self.fired.append(f"{rule}: {detail}")

    def note(self, rule: str, detail: str) -> None:
        self.notes.append(f"{NOTE_PREFIX}{rule}: {detail}")

    def as_json(self) -> str:
        return json.dumps(self.fired + self.notes)


def evaluate(conn: sqlite3.Connection) -> Verdict:
    """Evaluate every AQI flag rule against the recent observation series."""
    v = Verdict()
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=SERIES_WINDOW_HOURS)).isoformat()

    pm = conn.execute(
        """
        SELECT collected_at, aqi FROM observations
        WHERE collected_at >= ? AND parameter = ? AND aqi IS NOT NULL
        ORDER BY collected_at ASC
        """,
        (cutoff, PM25),
    ).fetchall()

    # Rule 1 — PM2.5 at or above Unhealthy for Sensitive Groups.
    worst = max(pm, key=lambda r: r["aqi"], default=None)
    if worst is not None and worst["aqi"] >= PM25_UNHEALTHY_SENSITIVE:
        v.fire("pm25_unhealthy_sensitive",
               f"AQI {worst['aqi']} at {worst['collected_at']}")

    # Rule 2 — PM2.5 rising 20+ points within any 3-hour span.
    # Compared pairwise across the series rather than first-to-last: a rise
    # that happened and then partly receded still crossed the threshold.
    rise_window = timedelta(hours=PM25_RISE_WINDOW_HOURS)
    for i, later in enumerate(pm):
        t_later = _parse(later["collected_at"])
        if t_later is None:
            continue
        for earlier in reversed(pm[:i]):
            t_earlier = _parse(earlier["collected_at"])
            if t_earlier is None:
                continue
            if t_later - t_earlier > rise_window:
                break
            if later["aqi"] - earlier["aqi"] >= PM25_RISE_POINTS:
                v.fire("pm25_rising",
                       f"{earlier['aqi']} -> {later['aqi']} within "
                       f"{PM25_RISE_WINDOW_HOURS:.0f}h ending {later['collected_at']}")
                break
        else:
            continue
        break

    # Rule 3 — a jump from Good into elevated Moderate between consecutive
    # readings (>=75 now, <=50 previously).
    for prev, cur in zip(pm, pm[1:]):
        if cur["aqi"] >= PM25_JUMP_TO and prev["aqi"] <= PM25_JUMP_FROM:
            v.fire("pm25_sudden_jump",
                   f"{prev['aqi']} -> {cur['aqi']} at {cur['collected_at']}")
            break

    # Rule 4 — any parameter reaching the Unhealthy category, not just PM2.5.
    row = conn.execute(
        """
        SELECT collected_at, parameter, category_number, aqi FROM observations
        WHERE collected_at >= ? AND category_number IS NOT NULL
          AND category_number >= ?
        ORDER BY category_number DESC LIMIT 1
        """,
        (cutoff, CATEGORY_UNHEALTHY),
    ).fetchone()
    if row:
        v.fire("category_unhealthy",
               f"{row['parameter']} category {row['category_number']} "
               f"(AQI {row['aqi']}) at {row['collected_at']}")

    # Rule 5 — the collector has stopped. A data-quality flag, not a
    # condition finding: every rule above reads a window that is empty when
    # nothing has been collected, so without this a dead collector evaluates
    # as clean air. Mirrors River's collector_stale. Added 2026-10-07, when
    # the AQI collector had stored nothing for fifteen days and every rule
    # here was silent about it.
    newest = conn.execute(
        "SELECT MAX(collected_at) AS t FROM observations"
    ).fetchone()
    if newest is None or newest["t"] is None:
        v.fire("collector_never_polled", "no observations")
    else:
        t = _parse(newest["t"])
        if t is not None:
            age = (now - t).total_seconds() / 3600.0
            if age > COLLECTOR_STALE_AFTER_HOURS:
                v.fire("collector_stale",
                       f"newest observation {age:.1f}h old "
                       f"(>{COLLECTOR_STALE_AFTER_HOURS:g}h)")

    # Note — the monitor behind a pollutant changed. AirNow's current service
    # returns the closest monitor reporting each pollutant, by straight-line
    # distance. When that monitor misses an hour it silently substitutes the
    # next closest, and a series that moves from Vallejo to Sebastopol (west
    # of the Mayacamas, a different airshed) has changed place, not air. A
    # note, never a flag: the switch is a fact about provenance, and readings
    # either side of it should not be compared as if from one instrument.
    # Rows from before 2026-10-08 carry no site_name and are skipped — an
    # unrecorded monitor is not a different one.
    _note_monitor_changes(conn, v, cutoff)

    return v


def _note_monitor_changes(conn: sqlite3.Connection, v: Verdict,
                          cutoff: str) -> None:
    rows = conn.execute(
        """
        SELECT parameter, site_name, collected_at FROM observations
        WHERE collected_at >= ? AND site_name IS NOT NULL AND site_name != ''
        ORDER BY parameter, collected_at ASC
        """,
        (cutoff,),
    ).fetchall()
    by_param: dict[str, list] = {}
    for r in rows:
        by_param.setdefault(r["parameter"], []).append(r)
    for param, series in sorted(by_param.items()):
        changes = [(a["site_name"], b["site_name"], b["collected_at"])
                   for a, b in zip(series, series[1:])
                   if a["site_name"] != b["site_name"]]
        if not changes:
            continue
        shown = "; ".join(f"{old} -> {new} at {at[:16]} UTC"
                          for old, new, at in changes[-3:])
        more = f" (latest 3 of {len(changes)})" if len(changes) > 3 else ""
        v.note("monitor_changed",
               f"{param} {len(changes)}x in {SERIES_WINDOW_HOURS:g}h: "
               f"{shown}{more}; now {series[-1]['site_name']}")


def _parse(ts: str):
    """Collector timestamps are ISO8601 UTC, but a malformed row should skip
    the comparison rather than take down the whole evaluation."""
    try:
        parsed = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
