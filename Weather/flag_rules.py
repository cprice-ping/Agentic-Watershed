"""
Deterministic flag rules — Weather
----------------------------------
Evaluates agent.py's fire-weather and flood flag criteria in code. Every one
of them is a numeric comparison over columns the collector already stores, or
the presence of a named NWS alert.

Weather is the domain where the local-SLM trial failed worst (CONTEXT.md,
2026-07-16): qwen3.5:4b flagged true against conditions meeting none of the
criteria — 60.8°F, 72.2% humidity, no active alerts — justifying it with
"compound risk factors" that appear nowhere in the prompt. The finding
recorded at the time was that the failure did not correlate with task
difficulty, on the domain whose rules are the simplest. Rules this mechanical
are a poor use of a language model's judgment.

Currently SHADOW ONLY — recorded alongside the model's own flagged value,
never overriding it.

The prompt is explicit that these apply "at ANY point in the current reading
OR the 48-hour trend data" — a lull in an ongoing wind event still warrants a
flag. The queries below therefore scan the window rather than the latest row.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

TREND_WINDOW_HOURS = 48.0

# Fire weather
TEMP_F_MIN        = 90.0
HUMIDITY_MAX_PCT  = 25.0   # only in combination with temp and wind
WIND_MPH_MIN      = 15.0   # only in that same combination
HUMIDITY_ALONE    = 15.0   # sufficient on its own
GUST_MPH_MIN      = 45.0

# Flood
PRECIP_1H_MM_MAX  = 25.0
PRECIP_24H_MM_MAX = 50.0

FIRE_ALERT_EVENTS  = ("red flag warning", "fire weather watch")
FLOOD_ALERT_EVENTS = ("flood watch", "flood warning")


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
    """Evaluate every Weather flag rule over the 48h trend window."""
    v = Verdict()
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=TREND_WINDOW_HOURS)).isoformat()

    # --- Active alerts -----------------------------------------------------
    # Matched on the event name rather than severity: the prompt names these
    # specific products, and severity is set per-alert by the issuing office.
    alerts = conn.execute(
        "SELECT DISTINCT event FROM alerts WHERE collected_at >= ?", (cutoff,)
    ).fetchall()
    for a in alerts:
        event = (a["event"] or "").strip()
        low = event.lower()
        if any(name in low for name in FIRE_ALERT_EVENTS):
            v.fire("fire_weather_alert", event)
        if any(name in low for name in FLOOD_ALERT_EVENTS):
            v.fire("flood_alert", event)

    # --- Fire weather thresholds ------------------------------------------
    # The three-part condition has to be evaluated per row: temp, humidity and
    # wind must be met by the *same* observation, not by three different ones
    # that each happened to cross once during the window.
    row = conn.execute(
        """
        SELECT collected_at, temperature_f, humidity_pct, wind_speed_mph
        FROM observations
        WHERE collected_at >= ?
          AND temperature_f  >= ? AND humidity_pct   <= ?
          AND wind_speed_mph >= ?
        ORDER BY collected_at DESC LIMIT 1
        """,
        (cutoff, TEMP_F_MIN, HUMIDITY_MAX_PCT, WIND_MPH_MIN),
    ).fetchone()
    if row:
        v.fire("hot_dry_windy",
               f"{row['temperature_f']:.0f}F / {row['humidity_pct']:.0f}% / "
               f"{row['wind_speed_mph']:.0f}mph at {row['collected_at']}")

    row = conn.execute(
        """
        SELECT collected_at, humidity_pct FROM observations
        WHERE collected_at >= ? AND humidity_pct IS NOT NULL AND humidity_pct <= ?
        ORDER BY humidity_pct ASC LIMIT 1
        """,
        (cutoff, HUMIDITY_ALONE),
    ).fetchone()
    if row:
        v.fire("humidity_critical", f"{row['humidity_pct']:.0f}% at {row['collected_at']}")

    row = conn.execute(
        """
        SELECT collected_at, wind_gust_mph FROM observations
        WHERE collected_at >= ? AND wind_gust_mph IS NOT NULL AND wind_gust_mph >= ?
        ORDER BY wind_gust_mph DESC LIMIT 1
        """,
        (cutoff, GUST_MPH_MIN),
    ).fetchone()
    if row:
        v.fire("wind_gust", f"{row['wind_gust_mph']:.0f}mph at {row['collected_at']}")

    # --- Flood thresholds --------------------------------------------------
    for column, limit, rule in (("precip_1h_mm",  PRECIP_1H_MM_MAX,  "precip_1h"),
                                ("precip_24h_mm", PRECIP_24H_MM_MAX, "precip_24h")):
        row = conn.execute(
            f"""
            SELECT collected_at, {column} AS v FROM observations
            WHERE collected_at >= ? AND {column} IS NOT NULL AND {column} > ?
            ORDER BY {column} DESC LIMIT 1
            """,
            (cutoff, limit),
        ).fetchone()
        if row:
            v.fire(rule, f"{row['v']:.1f}mm at {row['collected_at']}")

    return v
