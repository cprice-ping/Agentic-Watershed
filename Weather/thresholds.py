"""
Flag thresholds — Weather
-------------------------
The single definition of what counts as fire-weather or flood risk at this
node. Everything on the node that needs these numbers imports them from
here: agent.py builds its prompt criteria from them, mcp_server.py reports
them alongside the measurements, and flag_rules.py evaluates them.

Why this module exists. The same thresholds used to be written out four
times — in agent.py's prompt, in mcp_server.py's get_fire_risk_indicators
response, in flag_rules.py, and in Synthesis's constants — and three of them
drifted. get_fire_risk_indicators was telling the model "critical_humidity
≤ 20%, high_wind ≥ 20 mph, critical_wind ≥ 35 mph" while the prompt, the
rules and Synthesis all used 25% / 15 mph / 45 mph gusts. That block sat
inside the same JSON as the measured data, so it read as fact rather than as
instruction, and the model had been reconciling two humidity thresholds by
quoting both — the recurring "well below critical 15% and 20% thresholds"
line in published summaries.

Synthesis's copy is deliberately NOT consolidated here; see the note on
FIRE_WX_* in Synthesis/agent/agent_atproto.py. It grades the node's output
and must not adopt the node's definition of success.

Changing a number here changes the prompt, the tool output and the shadow
verdict together. That is the point.
"""

# --- Fire weather ----------------------------------------------------------
# The three-part combination, all of which must hold together.
TEMP_F_MIN       = 90.0
HUMIDITY_MAX_PCT = 25.0
WIND_MPH_MIN     = 15.0

# Either of these is sufficient on its own.
HUMIDITY_ALONE = 15.0
GUST_MPH_MIN   = 45.0

# --- Flood -----------------------------------------------------------------
PRECIP_1H_MM_MAX  = 25.0
PRECIP_24H_MM_MAX = 50.0

# --- Alerts ----------------------------------------------------------------
# Display names, matched case-insensitively by flag_rules. Matched on event
# name rather than severity: the prompt names these specific products, and
# severity is set per-alert by the issuing office.
FIRE_ALERT_EVENTS  = ("Red Flag Warning", "Fire Weather Watch")
FLOOD_ALERT_EVENTS = ("Flood Watch", "Flood Warning")

# --- Windows ---------------------------------------------------------------
# Criteria apply across this window, not only to the instantaneous reading:
# a lull during an ongoing wind event still warrants a flag.
TREND_WINDOW_HOURS = 48.0

# What counts as meaningful rain. Context for the model rather than a flag
# criterion.
MEANINGFUL_RAIN_1H_MM = 1.0

# How far back get_fire_risk_indicators reports on recent rain.
DRY_SPELL_LOOKBACK_DAYS = 7

# The dry-spell counter searches the whole observation record instead, with no
# lookback. A 7-day bound is why nothing could ever substantiate the "147+
# consecutive precipitation-free days" that synthesis summaries had been
# carrying forward in prose: the deepest true statement available was "none in
# the last 7 days", so the counter was incrementing itself across runs with
# nothing measuring it.
#
# The honest form of this number is bounded by the record, not by the climate.
# If no rain appears anywhere in the observations table, the answer is "none
# in the N days we have data for, and our data starts on <date>" — never a
# drought length. That distinction is the whole point of computing it.


def _n(value: float) -> str:
    """Render a threshold for human text: 90.0 as "90", 0.5 as "0.5"."""
    return f"{value:g}"


def fire_criteria_text() -> str:
    """The fire-weather criteria as prompt bullets, generated from the
    constants above so the prompt cannot drift from what is enforced."""
    return "\n".join([
        f"- Active {' or '.join(FIRE_ALERT_EVENTS)}",
        f"- Temperature ≥ {_n(TEMP_F_MIN)}°F AND humidity ≤ "
        f"{_n(HUMIDITY_MAX_PCT)}% AND wind ≥ {_n(WIND_MPH_MIN)} mph",
        f"- Humidity ≤ {_n(HUMIDITY_ALONE)}% regardless of other factors",
        f"- Wind gusts ≥ {_n(GUST_MPH_MIN)} mph",
    ])


def flood_criteria_text() -> str:
    """The flood criteria as prompt bullets."""
    return "\n".join([
        f"- Active {' or '.join(FLOOD_ALERT_EVENTS)}",
        f"- Precipitation > {_n(PRECIP_1H_MM_MAX)}mm in 1 hour",
        f"- Precipitation > {_n(PRECIP_24H_MM_MAX)}mm in 24 hours",
    ])


def as_dict() -> dict:
    """The criteria as structured values, for mcp_server.py to return next to
    the measurements. Phrased the same way the prompt phrases them so the
    model sees one rulebook, not two."""
    return {
        "combination": {
            "temperature_f": f"≥ {_n(TEMP_F_MIN)}",
            "humidity_pct": f"≤ {_n(HUMIDITY_MAX_PCT)}",
            "wind_mph": f"≥ {_n(WIND_MPH_MIN)}",
            "note": "all three together",
        },
        "sufficient_alone": {
            "humidity_pct": f"≤ {_n(HUMIDITY_ALONE)}",
            "wind_gust_mph": f"≥ {_n(GUST_MPH_MIN)}",
        },
        "alerts": list(FIRE_ALERT_EVENTS),
        "applies_over_hours": TREND_WINDOW_HOURS,
    }
