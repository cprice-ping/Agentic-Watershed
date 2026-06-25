"""
Watershed MCP Server
--------------------
Exposes Napa River gauge data (collected by collector.py) as MCP tools
that an agent harness can call via the Model Context Protocol.

Tools:
  get_recent_readings(n)              Last N readings across all stations
  get_readings_since(hours_ago)       Readings from the last N hours
  get_station_summary(station_id)     Latest values for a single station
  get_anomalies(threshold_pct)        Readings deviating from recent mean
  write_agent_observation(...)        Agent writes its own reasoning back to DB

Run (stdio, for Claude Desktop / agent harness):
  python mcp_server.py

Run (HTTP, for testing with MCP Inspector):
  python mcp_server.py --http
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — points at the same DB the collector writes to
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "watershed.db"

mcp = FastMCP(
    "watershed",
    instructions=(
        "You have access to real-time and historical Napa River gauge data "
        "from two USGS monitoring stations. Use these tools to understand "
        "current river conditions, identify anomalies, and record your "
        "observations. Always call get_recent_readings first to orient yourself."
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
def get_recent_readings(n: int = 20) -> str:
    """
    Return the most recent N readings across all stations and parameters.
    Use this first to get a sense of current conditions.

    Args:
        n: Number of readings to return (default 20, max 200)
    """
    n = min(n, 200)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, station_name, parameter_name, value, unit, qualifier
            FROM readings
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    if not rows:
        return "No readings in database yet. Run the collector first."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_readings_since(hours_ago: float = 24.0) -> str:
    """
    Return all readings from the last N hours.
    Useful for trend analysis and spotting changes over a time window.

    Args:
        hours_ago: How many hours back to look (default 24)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT collected_at, station_id, station_name,
                   parameter_name, value, unit, qualifier
            FROM readings
            WHERE collected_at >= ?
            ORDER BY station_id, parameter_code, collected_at
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return f"No readings found in the last {hours_ago} hours."
    return json.dumps(_rows_to_dicts(rows), indent=2)


@mcp.tool()
def get_station_summary(station_id: str = "11458000") -> str:
    """
    Return the latest reading for each parameter at a single station,
    plus a 7-day min/mean/max for context.

    Args:
        station_id: USGS station ID. Known stations:
                    11458000 = Napa River near Napa (default)
                    11456000 = Napa River near St Helena
    """
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db() as conn:
        # Latest value per parameter
        latest = conn.execute(
            """
            SELECT parameter_name, value, unit, qualifier, collected_at, usgs_datetime
            FROM readings
            WHERE station_id = ?
            GROUP BY parameter_code
            HAVING collected_at = MAX(collected_at)
            """,
            (station_id,),
        ).fetchall()

        # 7-day stats per parameter
        stats = conn.execute(
            """
            SELECT parameter_name, unit,
                   COUNT(*) as n_readings,
                   MIN(value) as min_val,
                   AVG(value) as mean_val,
                   MAX(value) as max_val
            FROM readings
            WHERE station_id = ? AND collected_at >= ? AND value IS NOT NULL
            GROUP BY parameter_code
            """,
            (station_id, cutoff_7d),
        ).fetchall()

    if not latest:
        return f"No data found for station {station_id}."

    result = {
        "station_id": station_id,
        "latest": _rows_to_dicts(latest),
        "seven_day_stats": _rows_to_dicts(stats),
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_anomalies(threshold_pct: float = 50.0, lookback_days: int = 30) -> str:
    """
    Find recent readings that deviate significantly from the rolling mean.
    Returns readings where the value differs from the mean by more than
    threshold_pct percent.

    Args:
        threshold_pct: Percentage deviation to flag as anomalous (default 50)
        lookback_days: How many days of history to compute the baseline mean from
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with _db() as conn:
        # Compute baseline mean per station+parameter over lookback window
        baselines = conn.execute(
            """
            SELECT station_id, parameter_code, parameter_name,
                   AVG(value) as mean_val, unit
            FROM readings
            WHERE collected_at >= ? AND value IS NOT NULL
            GROUP BY station_id, parameter_code
            """,
            (cutoff,),
        ).fetchall()

        # Get last 24h readings
        recent = conn.execute(
            """
            SELECT station_id, parameter_code, parameter_name,
                   value, unit, collected_at, qualifier
            FROM readings
            WHERE collected_at >= ? AND value IS NOT NULL
            ORDER BY collected_at DESC
            """,
            (recent_cutoff,),
        ).fetchall()

    # Build baseline lookup
    baseline_map = {
        (r["station_id"], r["parameter_code"]): r["mean_val"]
        for r in baselines
    }

    anomalies = []
    for row in recent:
        key = (row["station_id"], row["parameter_code"])
        mean = baseline_map.get(key)
        if mean is None or mean == 0:
            continue
        deviation_pct = abs(row["value"] - mean) / abs(mean) * 100
        if deviation_pct >= threshold_pct:
            d = dict(row)
            d["baseline_mean"] = round(mean, 3)
            d["deviation_pct"] = round(deviation_pct, 1)
            anomalies.append(d)

    if not anomalies:
        return f"No anomalies detected (>{threshold_pct}% deviation) in the last 24 hours."

    anomalies.sort(key=lambda x: x["deviation_pct"], reverse=True)
    return json.dumps(anomalies, indent=2)


@mcp.tool()
def write_agent_observation(
    summary: str,
    flagged: bool = False,
    reasoning: str = "",
) -> str:
    """
    Write the agent's observation and reasoning back to the database.
    Call this at the end of each agent run to persist conclusions.
    This forms the memory that future agent runs will read.

    Args:
        summary:   A concise human-readable summary of current conditions
                   (1-3 sentences). This is what future runs will see first.
        flagged:   True if conditions warrant attention or follow-up.
        reasoning: The agent's full reasoning, including which data points
                   drove the conclusion. Can be longer.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO agent_observations
                (observed_at, summary, flagged, reasoning)
            VALUES (?, ?, ?, ?)
            """,
            (now, summary, int(flagged), reasoning),
        )
        conn.commit()
    return json.dumps({"status": "ok", "observed_at": now})


@mcp.tool()
def get_recent_observations(n: int = 5) -> str:
    """
    Return the agent's most recent written observations.
    Call this at the start of each run for continuity — this is your memory
    of what previous runs concluded.

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
        return "No previous observations recorded. This appears to be a fresh run."
    return json.dumps(_rows_to_dicts(rows), indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Watershed MCP Server")
    parser.add_argument("--http", action="store_true", help="Run over HTTP (for Inspector)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
