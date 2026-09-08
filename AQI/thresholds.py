"""
Flag thresholds — AQI
---------------------
The single definition of what counts as an air-quality concern at this node.
agent.py builds its prompt criteria from these, mcp_server.py queries with
them, and flag_rules.py evaluates them. See Weather/thresholds.py for why
this pattern exists.

One reconciliation worth noting: get_smoke_indicators described its
"Unhealthy range" as AQI > 150 while the flag rule reads category_number ≥ 4.
Those are the same boundary — EPA's Unhealthy category starts at 151 — but
written as two unrelated-looking numbers in two files. PM25_UNHEALTHY_AQI
below is that boundary, stated once, with the category it corresponds to.
"""

PM25 = "PM2.5"

# Flags outright.
PM25_UNHEALTHY_SENSITIVE = 101   # EPA "Unhealthy for Sensitive Groups"

# A rise of this many AQI points inside the rise window flags.
PM25_RISE_POINTS       = 20
PM25_RISE_WINDOW_HOURS = 3.0

# A jump from Good into elevated Moderate flags, even below 101.
PM25_JUMP_TO   = 75   # current reading at or above this...
PM25_JUMP_FROM = 50   # ...when the previous was at or below this

# Any parameter reaching this EPA category flags.
CATEGORY_UNHEALTHY = 4

# The AQI value where CATEGORY_UNHEALTHY begins. Same boundary as the
# category rule, expressed in AQI points for the queries that work on the
# number rather than the category column.
PM25_UNHEALTHY_AQI = 151

# How much history the series tools return by default.
SERIES_WINDOW_HOURS = 24.0
TREND_WINDOW_DAYS   = 7


def _n(value: float) -> str:
    return f"{value:g}"


def flag_criteria_text() -> str:
    """The flag criteria as prompt bullets, generated from the constants so
    the prompt cannot drift from what is enforced."""
    return "\n".join([
        f"- PM2.5 AQI ≥ {PM25_UNHEALTHY_SENSITIVE} "
        f"(Unhealthy for Sensitive Groups or worse)",
        f"- PM2.5 AQI rising ≥ {PM25_RISE_POINTS} points in "
        f"{_n(PM25_RISE_WINDOW_HOURS)} hours",
        f"- PM2.5 AQI ≥ {PM25_JUMP_TO} AND previous observation was ≤ "
        f"{PM25_JUMP_FROM} (sudden jump from Good to elevated Moderate)",
        f"- Any category_number ≥ {CATEGORY_UNHEALTHY} (Unhealthy) "
        f"for any parameter",
    ])
