"""
Weather MCP Server
------------------
Exposes Napa County weather observations and NWS alerts as MCP tools.
Mirrors the watershed MCP server pattern exactly.

Tools:
  get_recent_observations(n)          Last N weather observations
  get_observations_since(hours_ago)   Observations in time window
  get_current_conditions()            Latest reading + 7-day stats
  get_active_alerts()                 Current NWS alerts for Napa County
  get_fire_risk_indicators()          Composite of conditions relevant to fire
  get_recent_observations(n)          Agent memory — past conclusions
  write_agent_observation(...)        Agent writes conclusion to DB
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

DB_PATH = Path(__file__).parent.parent / "data" / "weather.db"

mcp = FastMCP(
    "weather",
    instructions=(
        "You have access to Napa County weather observations from Napa County Airport (KAPC) "
        "and active NWS alerts. Use these tools to assess current meteorological conditions, "
        "fire weather risk, and precipitation patterns. Always call get_current_conditions "
        "first, then check get_active_alerts. For fire risk, call get_fire_risk_indicators."
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
def get_recent_observations(n: int = 10) -> str:
    """
    Return the most recent N weather observations from Napa County Airport.
    Includes temperature, humidity, wind speed/direction, gusts, precipitation.

    Args:
        n: Number of observations to return (default 10, max 100)
    """
    n = min(n, 100)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, obs_time, temperature_f, humidity_pct,
                   wind_speed_mph, wind_direction_deg, wind_gust_mph,
                   precip_1h_mm, text_description
            FROM observations
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    if not rows:
        return "No observations in database yet. Run the collector first."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_observations_since(hours_ago: float = 24.0) -> str:
    """
    Return all weather observations from the last N hours.
    Useful for spotting trends: temperature rise, humidity drop, wind shift.

    Args:
        hours_ago: How many hours back to look (default 24)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, obs_time, temperature_f, humidity_pct,
                   wind_speed_mph, wind_direction_deg, wind_gust_mph,
                   precip_1h_mm, precip_6h_mm, precip_24h_mm, text_description
            FROM observations
            WHERE collected_at >= ?
            ORDER BY collected_at ASC
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No observations found in the last {hours_ago} hours."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_current_conditions() -> str:
    """
    Return the latest weather observation plus 7-day statistical context.
    This is the primary orientation tool — call it first each run.
    """
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db() as conn:
        latest = conn.execute(
            """
            SELECT * FROM observations
            ORDER BY collected_at DESC
            LIMIT 1
            """
        ).fetchone()

        stats = conn.execute(
            """
            SELECT
                COUNT(*) as n_readings,
                MIN(temperature_f) as min_temp_f,
                AVG(temperature_f) as avg_temp_f,
                MAX(temperature_f) as max_temp_f,
                MIN(humidity_pct) as min_humidity,
                AVG(humidity_pct) as avg_humidity,
                MAX(wind_speed_mph) as max_wind_mph,
                MAX(wind_gust_mph) as max_gust_mph,
                SUM(precip_1h_mm) as total_precip_mm
            FROM observations
            WHERE collected_at >= ?
            """,
            (cutoff_7d,),
        ).fetchone()

    if not latest:
        return "No observations in database yet."

    result = {
        "current": dict(latest),
        "seven_day_stats": dict(stats) if stats else {},
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_active_alerts() -> str:
    """
    Return currently active NWS weather alerts for Napa County.
    Includes Red Flag Warnings, Fire Weather Watches, Flood Watches, etc.
    An empty result means no active alerts — explicitly note this in your reasoning.
    """
    # Alerts that haven't expired yet
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        # First try to get alerts that haven't expired
        rows = conn.execute(
            """
            SELECT event, severity, urgency, headline, onset, expires, zones
            FROM alerts
            WHERE (expires IS NULL OR expires > ?)
            GROUP BY alert_id
            ORDER BY collected_at DESC
            """,
            (now,),
        ).fetchall()

        # Fall back to most recent alerts regardless of expiry
        if not rows:
            rows = conn.execute(
                """
                SELECT event, severity, urgency, headline, onset, expires, zones
                FROM alerts
                ORDER BY collected_at DESC
                LIMIT 10
                """
            ).fetchall()

    if not rows:
        return "No active NWS alerts for Napa County. Conditions are not triggering any watches or warnings."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_fire_risk_indicators() -> str:
    """
    Return a composite view of fire weather indicators from recent observations.
    Surfaces the specific metrics most relevant to fire risk:
    temperature, humidity, wind speed, wind gusts, and recent precipitation.

    High fire risk indicators: temp > 90°F, humidity < 20%, wind > 20mph, no recent rain.
    """
    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db() as conn:
        latest = conn.execute(
            "SELECT temperature_f, humidity_pct, wind_speed_mph, wind_gust_mph, "
            "wind_direction_deg, precip_1h_mm, precip_24h_mm, obs_time "
            "FROM observations ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()

        # Trend over last 48h
        trend = conn.execute(
            """
            SELECT
                MIN(humidity_pct) as min_humidity_48h,
                AVG(humidity_pct) as avg_humidity_48h,
                MAX(temperature_f) as max_temp_48h,
                MAX(wind_speed_mph) as max_wind_48h,
                MAX(wind_gust_mph) as max_gust_48h,
                SUM(COALESCE(precip_1h_mm, 0)) as total_precip_48h_mm
            FROM observations
            WHERE collected_at >= ?
            """,
            (cutoff_48h,),
        ).fetchone()

        # Days since meaningful precipitation (>1mm)
        last_rain = conn.execute(
            """
            SELECT collected_at, precip_1h_mm
            FROM observations
            WHERE precip_1h_mm > 1.0 AND collected_at >= ?
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            (cutoff_7d,),
        ).fetchone()

    if not latest:
        return "No observations available."

    result = {
        "current": dict(latest),
        "trend_48h": dict(trend) if trend else {},
        "last_significant_rain": dict(last_rain) if last_rain else "None in last 7 days",
        "fire_risk_thresholds": {
            "high_temp": "≥ 90°F",
            "critical_humidity": "≤ 20%",
            "high_wind": "≥ 20 mph",
            "critical_wind": "≥ 35 mph",
        },
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_recent_agent_observations(n: int = 5) -> str:
    """
    Return the weather agent's most recent written observations (memory).
    Call this at the start of each run for continuity with previous conclusions.

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
    Write the weather agent's observation and reasoning to the database.
    Call this at the end of each run.

    Args:
        summary:   Concise current conditions summary for the next run to read.
                   Include temp, humidity, wind, and any notable patterns.
        flagged:   True if conditions warrant attention (fire risk, flood risk,
                   significant weather event).
        reasoning: Full reasoning including which thresholds were considered.
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
    parser = argparse.ArgumentParser(description="Weather MCP Server")
    parser.add_argument("--http", action="store_true", help="Run over HTTP")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
