"""
Reading a low-flow gauge — River
--------------------------------
Two properties of this station that every tool has to state, because nothing
in a bare number carries them and the agent cannot infer either.

THE RATING FLOOR. Discharge at a USGS gauge is not measured, it is derived
from stage through a rating curve. Below some stage the curve has no
resolution and reports exactly 0.00, which does not mean "no water": on
2026-09-10 the Napa gauge read 0.0 cfs across eleven consecutive readings
while stage sat at 2.04-2.06 ft, then reported 0.03 cfs when stage gained a
single hundredth of a foot. So one 0.01 ft step is worth 0.03 cfs down there,
and any arithmetic on a 0.0 is arithmetic on the instrument's floor rather
than on the river. `get_anomalies` computed "100% deviation from a 30-day
baseline of 0.826 cfs" from exactly this, and the agent reported "severe
drought conditions emerging".

THE DIEL CYCLE. Stage at this station oscillates once a day: maximum before
dawn, minimum in the afternoon, range about 0.11 ft across 2026-09-08 to
09-10. The cycle is measured. Its cause is not.

What the data does establish is that it is not tide: tide in this reach would
be mixed semidiurnal, two peaks a day, amplitude in feet. Beyond that, an
afternoon minimum is consistent with evapotranspiration and equally
consistent with a daily irrigation withdrawal — this is an agricultural
valley — and nothing in a stage series separates them. No cause is named in
anything the agent reads, deliberately: an explanation it cannot verify is an
explanation it will publish as fact.

The cause does not matter for the fix. What matters is that the cycle exists
and the agent was sampling it blind.

That cycle matters because the agent samples it at 00:00 and 12:00 Pacific,
which is near the peak and near the trough. Every consecutive pair of
observations therefore straddles opposite phases, and the agent compared each
run against its own previous one. Peak-to-trough reads as collapse and
trough-to-peak reads as recovery, forever, and neither is a trend. It went
unnoticed for weeks only because the absolute numbers were small enough that
the swing looked like noise — until the trough crossed the rating floor and
the same comparison produced a 100% change.

The fix in both cases is the same one this codebase keeps arriving at: send
the frame with the value instead of leaving it to be reconstructed downstream.
"""

from datetime import datetime, timedelta, timezone

# USGS reports discharge as exactly 0.00 when the rating curve yields no
# measurable flow. Compared with ==, not a tolerance: this is the literal
# value the feed sends, and treating anything near zero as "floor" would
# start discarding the real 0.03 readings that sit just above it.
DISCHARGE_FLOOR_CFS = 0.0

# Substring identifying discharge among USGS variableName labels
# ("Streamflow, ft³/s"). Matched loosely because the label is USGS's prose,
# not a code, and it has already changed once this month — it used to arrive
# HTML-escaped as "ft&#179;/s".
_DISCHARGE_LABELS = ("streamflow", "discharge")

# One full diel cycle. Used to bound the min/max envelope a reading is
# located within, and to find the same-phase reading a day earlier.
DIEL_PERIOD_HOURS = 24.0

# How close to a day ago the same-phase comparison has to land. The collector
# polls every 15 minutes, so an hour is generous; beyond it the two readings
# are far enough apart in the cycle that comparing them reintroduces the bug.
SAME_PHASE_TOLERANCE_HOURS = 1.0


def is_discharge(parameter_name: str | None) -> bool:
    return any(k in (parameter_name or "").lower() for k in _DISCHARGE_LABELS)


def at_floor(parameter_name: str | None, value) -> bool:
    """Whether this reading is discharge pinned at the rating curve's floor."""
    return (is_discharge(parameter_name) and value is not None
            and float(value) == DISCHARGE_FLOOR_CFS)


def floor_note(parameter_name: str | None, value) -> str | None:
    """A sentence to attach to a floor-pinned reading, or None."""
    if not at_floor(parameter_name, value):
        return None
    return ("at or below the gauge's measurable minimum — the rating curve "
            "reports 0.00 here, which is not a measurement of zero flow and "
            "does not mean the channel is dry")


def percent_change(new, old, parameter_name: str | None = None):
    """Percent change, or None where the figure would be meaningless.

    None when either side is at the rating floor. A percentage needs both a
    real numerator and a real denominator, and a floor reading is neither —
    the number it would produce ("100% below baseline") describes the
    instrument, not the river, while reading as though it described the river.
    """
    if old in (None, 0) or new is None:
        return None
    if at_floor(parameter_name, new) or at_floor(parameter_name, old):
        return None
    return (float(new) - float(old)) / abs(float(old)) * 100.0


def _parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def diel_frame(rows, parameter_name: str, current_value, current_at) -> dict:
    """Locate a reading inside the last 24 hours of its own parameter.

    *rows* are reading rows (any mapping with collected_at, parameter_name,
    value) covering at least the last day; ordering does not matter.

    Returns the day's min and max with their times, where the current reading
    sits between them as a 0-100 position, and the reading from roughly 24
    hours earlier — the same point in the cycle, which is the only comparison
    that says anything about a trend rather than about the time of day.
    """
    now = _parse(current_at)
    frame: dict = {}
    series = []
    for r in rows:
        if (r["parameter_name"] or "") != parameter_name or r["value"] is None:
            continue
        t = _parse(r["collected_at"])
        if t is None or now is None:
            continue
        age_h = (now - t).total_seconds() / 3600.0
        if 0 <= age_h <= DIEL_PERIOD_HOURS:
            series.append((t, float(r["value"]), age_h))

    if not series:
        return frame

    lo = min(series, key=lambda s: s[1])
    hi = max(series, key=lambda s: s[1])
    frame["window_hours"] = DIEL_PERIOD_HOURS
    frame["min"] = round(lo[1], 3)
    frame["min_at"] = lo[0].isoformat()
    frame["max"] = round(hi[1], 3)
    frame["max_at"] = hi[0].isoformat()

    if current_value is not None and hi[1] != lo[1]:
        pos = (float(current_value) - lo[1]) / (hi[1] - lo[1]) * 100.0
        frame["position_in_daily_range_pct"] = round(pos)
        frame["position_note"] = (
            "0 = the last 24 hours' lowest reading, 100 = its highest. This "
            "station cycles once a day (high before dawn, low in the "
            "afternoon), so a low position may mean the time of day rather "
            "than a change in the river.")
    elif current_value is not None:
        frame["position_note"] = (
            "flat across the last 24 hours — no daily range to place this in")

    # Same point in yesterday's cycle: the only like-for-like comparison.
    candidates = [s for s in series
                  if abs(s[2] - DIEL_PERIOD_HOURS) <= SAME_PHASE_TOLERANCE_HOURS]
    if candidates:
        best = min(candidates, key=lambda s: abs(s[2] - DIEL_PERIOD_HOURS))
        frame["same_phase_24h_ago"] = round(best[1], 3)
        frame["same_phase_at"] = best[0].isoformat()
        delta = percent_change(current_value, best[1], parameter_name)
        if delta is not None:
            frame["same_phase_change_pct"] = round(delta, 1)
        frame["same_phase_note"] = (
            "Compare against this, not against the previous agent run: runs "
            "are 12 hours apart and the cycle is 24, so consecutive runs "
            "always sit at opposite phases.")
    else:
        frame["same_phase_note"] = (
            "No reading close enough to 24 hours ago to compare like for "
            "like — the collector has a gap. Treat any day-over-day claim as "
            "unsupported.")
    return frame
