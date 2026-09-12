"""
Deterministic flag rules — River
--------------------------------
River had no rules module for a defensible reason: `floodStageThresholdFt` is
unconfigured for both Napa gauges, so there is no threshold for "the river is
high", and "the river is low" in September is the season rather than an event.
Nothing to evaluate.

The template-vs-agent comparison on 2026-09-12 changed that. Over 25 runs the
agent raised six flags. Five were September low water restated as "extreme",
"severe" and "warrants continued monitoring" — alarm without information, on
a prompt that already says a dry Napa summer is the norm. The sixth was real:

    2026-08-31 19:00 — streamflow 0.55 cfs at 17:45 rising to 8.97 cfs at
    19:00, a sixteen-fold increase inside two hours.

That one mattered, and the agent caught it. It also reported the gage height
as "19.00 ft" — the timestamp, 19:00 UTC, rendered as a river level, against
an actual 2.42 ft — and called a 1531% rise "~900%". So the single valuable
flag in 25 runs arrived with a reading nine times the river's real level.

A rate of change is arithmetic. This module computes it, which makes the one
thing the model demonstrably contributed available for free and with correct
numbers attached.

SHADOW ONLY, like the other three domains. The verdict is recorded next to
the model's own; it does not override it.

One difference from the other three, deliberate and worth stating because it
otherwise reads as an oversight. Weather, AQI and Fire generate their prompt
criteria from the same constants their rules evaluate, so the two sides are
measuring whether the model applied a threshold it was given. River's prompt
is NOT being told about SURGE_FACTOR. The agent found the 2026-08-31 event
unaided, and the question this shadow answers is whether it keeps doing that
— which it cannot answer if the criterion is handed over first. If the rule
is ever made authoritative, the prompt should be generated from these
constants at the same time and this paragraph deleted.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import hydrology

# A surge is a proportional jump within a short window.
#
# Both gates matter. Proportional alone fires constantly at the bottom of the
# rating curve, where 0.03 to 0.14 is a 367% rise and four hundredths of a
# cubic foot per second — the same defect Fire's frp_rising had, where any
# increase counted and the rule therefore fired on 12 of 13 runs. Absolute
# alone would miss a genuine surge on a small creek.
#
# 4.0 and 1.0 cfs against the observed event: 0.55 to 8.97 is 16.3x and 8.42
# cfs, clearing both by a wide margin. Against the noise: the largest
# non-event move in the same fortnight is 0.14 to 0.32, which is 2.3x and
# 0.18 cfs, and fails both.
SURGE_FACTOR = 4.0
SURGE_MIN_RISE_CFS = 1.0
SURGE_WINDOW_HOURS = 3.0

# A collector that has stopped makes every other rule's silence uninformative,
# the same data-quality flag Fire and Weather carry. Twelve missed polls at
# 15-minute cadence.
STALE_AFTER_HOURS = 3.0


class Verdict:
    """Outcome of evaluating the rules. `fired` names each rule that matched
    and the values that made it match, so a stored verdict can be read back
    later without re-running anything."""

    def __init__(self) -> None:
        self.fired: list[str] = []

    @property
    def must_flag(self) -> bool:
        return bool(self.fired)

    def fire(self, rule: str, detail: str) -> None:
        self.fired.append(f"{rule}: {detail}")

    def as_json(self) -> str:
        return json.dumps(self.fired)


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(conn: sqlite3.Connection) -> Verdict:
    """Evaluate every River flag rule against current collector data."""
    v = Verdict()
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    window = (now - timedelta(hours=SURGE_WINDOW_HOURS)).isoformat()

    # Rule 1 — discharge surging at any station.
    #
    # Compared within the window rather than against the previous agent run,
    # because a surge is a rate and the agent runs twelve hours apart. The
    # 2026-08-31 event rose and fell inside a single afternoon; a
    # run-to-run comparison would have straddled it.
    rows = conn.execute(
        """
        SELECT station_id, station_name, parameter_code, parameter_name,
               value, collected_at
        FROM readings
        WHERE collected_at >= ? AND value IS NOT NULL
        ORDER BY station_id, parameter_code, collected_at
        """,
        (window,),
    ).fetchall()
    # Keyed on the parameter as well as the station. Both Napa gauges report
    # stage alongside discharge, and a series that mixed two parameters would
    # compare a trough in one against a peak in the other.
    by_series: dict[tuple, list] = {}
    for r in rows:
        if not hydrology.is_discharge(r["parameter_name"]):
            continue
        key = (r["station_id"], r["station_name"], r["parameter_code"])
        by_series.setdefault(key, []).append(r)

    for (sid, sname, _code), series in by_series.items():
        if len(series) < 2:
            continue
        low = min(series, key=lambda r: r["value"])
        # Only a rise counts, so the peak must come after the trough.
        after = [r for r in series if r["collected_at"] > low["collected_at"]]
        if not after:
            continue
        high = max(after, key=lambda r: r["value"])
        rise = float(high["value"]) - float(low["value"])
        # A floor reading is not a denominator — the ratio against 0.0 is
        # undefined, and against the floor it describes the rating curve.
        # The absolute rise still applies, which is what catches a surge
        # starting from a dry channel.
        factor = (float(high["value"]) / float(low["value"])
                  if low["value"] and not hydrology.at_floor(
                      low["parameter_name"], low["value"]) else None)
        if rise >= SURGE_MIN_RISE_CFS and (factor is None
                                           or factor >= SURGE_FACTOR):
            v.fire("discharge_surge",
                   f"{sname} {low['value']:g} -> {high['value']:g} cfs "
                   + (f"(x{factor:.1f}) " if factor is not None
                      else "(from the rating floor) ")
                   + f"within {SURGE_WINDOW_HOURS:g}h, "
                   f"ending {high['collected_at'][:19]}")

    # Rule 2 — the collector has stopped. A data-quality flag, distinct from a
    # condition finding: it means the absence of a surge is uninformative.
    newest = conn.execute(
        "SELECT MAX(collected_at) AS t FROM readings"
    ).fetchone()
    if newest is None or newest["t"] is None:
        v.fire("collector_never_polled", "no readings")
    else:
        t = _parse(newest["t"])
        if t is not None:
            age = (now - t).total_seconds() / 3600.0
            if age > STALE_AFTER_HOURS:
                v.fire("collector_stale",
                       f"newest reading {age:.1f}h old (>{STALE_AFTER_HOURS:g}h)")

    return v


# Deliberately NOT implemented: low flow.
#
# There is no threshold for it. floodStageThresholdFt is unconfigured, a Napa
# summer is rainless by default, and both gauges sit at or near the rating
# floor from July to October. Every low-water flag the agent raised in the
# 2026-09-12 comparison — "extreme low-flow state", "severe extended low-flow
# state", "multi-day suppression" — described the season. Encoding a
# threshold for it would be inventing a number to justify an alarm, which is
# the failure this file exists to avoid rather than automate.
