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
from datetime import datetime, timedelta, timezone

PM25 = "PM2.5"

PM25_UNHEALTHY_SENSITIVE = 101   # AQI >= this flags outright
PM25_RISE_POINTS         = 20    # over the rise window
PM25_RISE_WINDOW_HOURS   = 3.0
PM25_JUMP_TO             = 75    # >= this, when the previous reading was...
PM25_JUMP_FROM           = 50    # ...<= this
CATEGORY_UNHEALTHY       = 4     # any parameter

SERIES_WINDOW_HOURS = 24.0


class Verdict:
    def __init__(self) -> None:
        self.fired: list[str] = []

    @property
    def must_flag(self) -> bool:
        return bool(self.fired)

    def fire(self, rule: str, detail: str) -> None:
        self.fired.append(f"{rule}: {detail}")

    def as_json(self) -> str:
        return json.dumps(self.fired)


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

    return v


def _parse(ts: str):
    """Collector timestamps are ISO8601 UTC, but a malformed row should skip
    the comparison rather than take down the whole evaluation."""
    try:
        parsed = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
