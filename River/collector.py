"""
Watershed Collector
-------------------
Polls USGS Instantaneous Values API for Napa River gauge stations
and stores readings into a local SQLite database.

Designed to run on a schedule (cron) or continuously with --loop.

Stations:
  11458000  Napa River near Napa (main gauge)
  11456000  Napa River near St Helena (upstream)

Parameters collected:
  00060  Discharge (cfs)
  00065  Gage height (ft)

Usage:
  python collector.py           # single poll
  python collector.py --loop    # poll every 15 minutes
  python collector.py --init    # initialise DB only
"""

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration  (location-specific values come from node_config.json)
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "watershed.db"

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
STATIONS  = _NODE_CFG["watershed"]["usgs_stations"]

# USGS Instantaneous Values endpoint (legacy — stable, widely used)
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"

PARAMETERS = {
    "00060": {"name": "discharge", "unit": "cfs", "label": "Discharge"},
    "00065": {"name": "gage_height", "unit": "ft", "label": "Gage Height"},
}

POLL_INTERVAL_SECONDS = 15 * 60  # 15 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("watershed.collector")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at    TEXT NOT NULL,          -- ISO8601 UTC
            station_id      TEXT NOT NULL,
            station_name    TEXT NOT NULL,
            parameter_code  TEXT NOT NULL,
            parameter_name  TEXT NOT NULL,
            value           REAL,
            unit            TEXT NOT NULL,
            qualifier       TEXT,                   -- e.g. "Ice", "Eqp"
            usgs_datetime   TEXT                    -- timestamp from USGS
        );

        CREATE INDEX IF NOT EXISTS idx_readings_station_time
            ON readings (station_id, collected_at DESC);

        CREATE TABLE IF NOT EXISTS agent_observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at     TEXT NOT NULL,
            summary         TEXT NOT NULL,
            flagged         INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
            reasoning       TEXT,
            raw_context     TEXT                         -- JSON snapshot agent used
        );
    """)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# USGS fetch
# ---------------------------------------------------------------------------

def fetch_usgs(station_ids: list[str], param_codes: list[str]) -> dict:
    """
    Call the USGS IV service for one or more stations and parameters.
    Returns the parsed JSON response.
    """
    params = {
        "format": "json",
        "sites": ",".join(station_ids),
        "parameterCd": ",".join(param_codes),
        "siteStatus": "active",
    }
    resp = httpx.get(USGS_IV_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_usgs_response(data: dict) -> list[dict]:
    """
    Extract individual readings from the USGS JSON envelope.
    Returns a flat list of dicts ready for DB insertion.
    """
    rows = []
    now = datetime.now(timezone.utc).isoformat()

    time_series_list = (
        data.get("value", {}).get("timeSeries", [])
    )

    for ts in time_series_list:
        site_code = ts["sourceInfo"]["siteCode"][0]["value"]
        site_name = ts["sourceInfo"]["siteName"]
        param_code = ts["variable"]["variableCode"][0]["value"]
        param_name = ts["variable"]["variableName"]
        unit = ts["variable"]["unit"]["unitCode"]

        # Take the most recent value only
        values = ts.get("values", [{}])[0].get("value", [])
        if not values:
            log.warning("No values for station %s param %s", site_code, param_code)
            continue

        latest = values[-1]
        raw_value = latest.get("value")
        # USGS qualifiers are plain strings in the IV API (e.g. ["P", "Ice"]), not dicts
        qualifiers = latest.get("qualifiers", [])
        qualifier = ",".join(q if isinstance(q, str) else q.get("qualifierCode", "") for q in qualifiers)

        # USGS uses -999999 as no-data sentinel
        value = float(raw_value) if raw_value and float(raw_value) != -999999 else None

        rows.append({
            "collected_at": now,
            "station_id": site_code,
            "station_name": site_name,
            "parameter_code": param_code,
            "parameter_name": param_name,
            "value": value,
            "unit": unit,
            "qualifier": qualifier or None,
            "usgs_datetime": latest.get("dateTime"),
        })

    return rows


def store_readings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO readings
            (collected_at, station_id, station_name, parameter_code,
             parameter_name, value, unit, qualifier, usgs_datetime)
        VALUES
            (:collected_at, :station_id, :station_name, :parameter_code,
             :parameter_name, :value, :unit, :qualifier, :usgs_datetime)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll(conn: sqlite3.Connection) -> None:
    station_ids = list(STATIONS.keys())
    param_codes = list(PARAMETERS.keys())

    log.info("Polling USGS for stations: %s", station_ids)
    try:
        data = fetch_usgs(station_ids, param_codes)
    except httpx.HTTPError as exc:
        log.error("USGS fetch failed: %s", exc)
        return

    rows = parse_usgs_response(data)
    count = store_readings(conn, rows)
    log.info("Stored %d reading(s)", count)

    for row in rows:
        flag = " ⚠️" if row["qualifier"] else ""
        val = f"{row['value']:.2f}" if row["value"] is not None else "N/A"
        log.info(
            "  %s | %s | %s %s%s",
            STATIONS.get(row["station_id"], row["station_id"]),
            row["parameter_name"],
            val,
            row["unit"],
            flag,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Watershed data collector")
    parser.add_argument("--init", action="store_true", help="Initialise DB and exit")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB")
    args = parser.parse_args()

    db_path = Path(args.db)
    conn = get_db(db_path)
    init_db(conn)

    if args.init:
        return

    if args.loop:
        log.info("Running in loop mode, polling every %ds", POLL_INTERVAL_SECONDS)
        while True:
            poll(conn)
            time.sleep(POLL_INTERVAL_SECONDS)
    else:
        poll(conn)


if __name__ == "__main__":
    main()
