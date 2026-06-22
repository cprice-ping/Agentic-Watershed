"""
AQI MCP Server
--------------
Exposes Napa County air quality data (PM2.5 and Ozone) as MCP tools.
Same pattern as watershed and weather MCP servers.

Tools:
  get_current_aqi()                   Latest readings for all parameters
  get_aqi_since(hours_ago)            Time-windowed readings
  get_aqi_trend(days)                 Daily trend — rising/falling/stable
  get_smoke_indicators()              PM2.5-focused view for fire/smoke detection
  get_recent_agent_observations(n)    Agent memory
  write_agent_observation(...)        Agent writes conclusion
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "data" / "aqi.db"

mcp = FastMCP(
    "aqi",
    instructions=(
        "You have access to Napa County air quality data — PM2.5 and Ozone AQI readings. "
        "PM2.5 is the primary wildfire smoke indicator: a sudden AQI rise, especially "
        "when weather conditions don't explain it, often means a fire has started upwind. "
        "Always call get_current_aqi first, then get_smoke_indicators for fire context. "
        "AQI categories: 1=Good(0-50), 2=Moderate(51-100), 3=USG(101-150), "
        "4=Unhealthy(151-200), 5=Very Unhealthy(201-300), 6=Hazardous(301+)."
    ),
)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_current_aqi() -> str:
    """
    Return the most recent AQI reading for each parameter (PM2.5 and Ozone).
    This is your orientation tool — call it first each run.
    Includes the AQI value, category name, and when the observation was taken.
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT parameter, aqi, category_number, category_name,
                   obs_date, obs_hour, reporting_area, collected_at
            FROM observations
            GROUP BY parameter
            HAVING collected_at = MAX(collected_at)
            ORDER BY parameter
            """
        ).fetchall()

    if not rows:
        return "No AQI observations in database yet. Run the collector first."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_aqi_since(hours_ago: float = 24.0) -> str:
    """
    Return all AQI observations from the last N hours, ordered by time.
    Use this to see if AQI is rising, falling, or stable.

    Args:
        hours_ago: How many hours back to look (default 24)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, parameter, aqi, category_name, obs_date, obs_hour
            FROM observations
            WHERE collected_at >= ?
            ORDER BY parameter, collected_at ASC
            """,
            (cutoff,),
        ).fetchall()

    if not rows:
        return f"No observations found in the last {hours_ago} hours."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_aqi_trend(days: int = 7) -> str:
    """
    Return daily min/mean/max AQI per parameter over the last N days.
    Useful for identifying sustained degradation vs a single spike.

    Args:
        days: Number of days of history to summarise (default 7)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT
                DATE(collected_at) as date,
                parameter,
                MIN(aqi) as min_aqi,
                ROUND(AVG(aqi), 1) as avg_aqi,
                MAX(aqi) as max_aqi,
                MAX(category_number) as worst_category,
                COUNT(*) as n_readings
            FROM observations
            WHERE collected_at >= ? AND aqi IS NOT NULL
            GROUP BY DATE(collected_at), parameter
            ORDER BY date ASC, parameter
            """,
            (cutoff,),
        ).fetchall()

    if not rows:
        return f"No observations found in the last {days} days."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_smoke_indicators() -> str:
    """
    Return a smoke/fire-focused view of recent PM2.5 data.
    PM2.5 is the most sensitive early indicator of wildfire smoke.

    Surfaces:
    - Current PM2.5 AQI and category
    - Whether PM2.5 is rising rapidly (>20 AQI points in 3 hours)
    - Peak PM2.5 in last 24h and 7 days
    - Any readings in Unhealthy range (AQI > 150) in last 7 days

    A sudden PM2.5 spike without corresponding ozone rise often means
    smoke from a nearby fire rather than general pollution.
    """
    now = datetime.now(timezone.utc)
    cutoff_3h = (now - timedelta(hours=3)).isoformat()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    with _db() as conn:
        # Latest PM2.5
        current = conn.execute(
            """
            SELECT aqi, category_number, category_name, collected_at, obs_hour
            FROM observations
            WHERE parameter = 'PM2.5'
            ORDER BY collected_at DESC LIMIT 1
            """
        ).fetchone()

        # PM2.5 3 hours ago (for rate-of-change)
        three_hours_ago = conn.execute(
            """
            SELECT aqi, collected_at
            FROM observations
            WHERE parameter = 'PM2.5' AND collected_at <= ?
            ORDER BY collected_at DESC LIMIT 1
            """,
            (cutoff_3h,),
        ).fetchone()

        # 24h peak
        peak_24h = conn.execute(
            """
            SELECT MAX(aqi) as peak_aqi, MAX(category_number) as worst_cat
            FROM observations
            WHERE parameter = 'PM2.5' AND collected_at >= ?
            """,
            (cutoff_24h,),
        ).fetchone()

        # 7d peak
        peak_7d = conn.execute(
            """
            SELECT MAX(aqi) as peak_aqi, DATE(collected_at) as peak_date
            FROM observations
            WHERE parameter = 'PM2.5' AND collected_at >= ?
            """,
            (cutoff_7d,),
        ).fetchone()

        # Any unhealthy readings in last 7 days
        unhealthy = conn.execute(
            """
            SELECT COUNT(*) as n, MAX(aqi) as worst_aqi
            FROM observations
            WHERE parameter = 'PM2.5' AND aqi > 150 AND collected_at >= ?
            """,
            (cutoff_7d,),
        ).fetchone()

        # Latest Ozone for comparison
        ozone = conn.execute(
            """
            SELECT aqi, category_name
            FROM observations
            WHERE parameter = 'OZONE'
            ORDER BY collected_at DESC LIMIT 1
            """
        ).fetchone()

    if not current:
        return "No PM2.5 data available yet."

    # Calculate rate of change
    aqi_change = None
    rising_rapidly = False
    if current and three_hours_ago and current["aqi"] and three_hours_ago["aqi"]:
        aqi_change = current["aqi"] - three_hours_ago["aqi"]
        rising_rapidly = aqi_change >= 20

    result = {
        "pm25_current": dict(current) if current else None,
        "pm25_change_3h": aqi_change,
        "pm25_rising_rapidly": rising_rapidly,
        "pm25_peak_24h": dict(peak_24h) if peak_24h else None,
        "pm25_peak_7d": dict(peak_7d) if peak_7d else None,
        "pm25_unhealthy_readings_7d": dict(unhealthy) if unhealthy else None,
        "ozone_current": dict(ozone) if ozone else None,
        "interpretation_note": (
            "A PM2.5 spike without ozone rise = likely smoke. "
            "Both rising = heat/pollution combo. "
            "Ozone only = summer photochemical smog, not fire."
        ),
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_recent_agent_observations(n: int = 5) -> str:
    """
    Return the AQI agent's most recent observations (memory).
    Call this at the start of each run for continuity.

    Args:
        n: Number of past observations to retrieve (default 5)
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT observed_at, summary, flagged, reasoning
            FROM agent_observations
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()

    if not rows:
        return "No previous observations recorded. This is a fresh run."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def write_agent_observation(
    summary: str,
    flagged: bool = False,
    reasoning: str = "",
) -> str:
    """
    Write the AQI agent's observation and reasoning to the database.
    Call this at the end of each run.

    Args:
        summary:   Concise summary for the next run to read.
                   Include current PM2.5 AQI, category, and any trend.
        flagged:   True if AQI is elevated, rising rapidly, or smoke is suspected.
        reasoning: Full reasoning including which thresholds were evaluated.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO agent_observations (observed_at, summary, flagged, reasoning)
            VALUES (?, ?, ?, ?)
            """,
            (now, summary, int(flagged), reasoning),
        )
        conn.commit()
    return json.dumps({"status": "ok", "observed_at": now})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AQI MCP Server")
    parser.add_argument("--http", action="store_true", help="Run over HTTP")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
