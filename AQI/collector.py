"""
AQI Collector
-------------
Polls the AirNow API for current air quality observations for Napa County.
Collects AQI, PM2.5, and Ozone — the three indicators most relevant to
wildfire smoke detection and general air quality.

Requires a free AirNow API key: https://docs.airnowapi.org/login
Set via environment variable: AIRNOW_API_KEY=...

AirNow updates observations once per hour, so polling every 30 minutes
is sufficient and respectful of the service.

Usage:
  python collector.py           # single poll
  python collector.py --loop    # poll every 30 minutes
  python collector.py --init    # initialise DB only
"""

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "aqi.db"

AIRNOW_BASE = "https://www.airnowapi.org"

# Napa, CA — city centre coordinates
NAPA_LAT = 38.2975
NAPA_LON = -122.2869

# Search radius in miles — 25 miles covers the whole valley
DISTANCE_MILES = 25

# Parameters to collect
PARAMETERS = "PM25,OZONE"

POLL_INTERVAL_SECONDS = 30 * 60  # 30 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("aqi.collector")

# AQI category thresholds for reference
AQI_CATEGORIES = {
    1: "Good",
    2: "Moderate",
    3: "Unhealthy for Sensitive Groups",
    4: "Unhealthy",
    5: "Very Unhealthy",
    6: "Hazardous",
}


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
        CREATE TABLE IF NOT EXISTS observations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at        TEXT NOT NULL,      -- ISO8601 UTC, when we fetched
            obs_date            TEXT,               -- date from AirNow
            obs_hour            INTEGER,            -- hour from AirNow (local)
            reporting_area      TEXT,               -- e.g. "Napa"
            state_code          TEXT,
            latitude            REAL,
            longitude           REAL,
            parameter           TEXT,               -- PM2.5 or OZONE
            aqi                 INTEGER,
            category_number     INTEGER,
            category_name       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_aqi_time
            ON observations (collected_at DESC);

        CREATE INDEX IF NOT EXISTS idx_aqi_param
            ON observations (parameter, collected_at DESC);

        CREATE TABLE IF NOT EXISTS agent_observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at     TEXT NOT NULL,
            summary         TEXT NOT NULL,
            flagged         INTEGER NOT NULL DEFAULT 0,
            reasoning       TEXT
        );
    """)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# AirNow fetch
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = os.environ.get("AIRNOW_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "AIRNOW_API_KEY environment variable not set. "
            "Get a free key at https://docs.airnowapi.org/login"
        )
    return key


def fetch_observations(api_key: str) -> list[dict]:
    """
    Fetch current AQI observations for Napa by lat/lon.
    Returns a list of parameter records (one per pollutant).
    """
    params = {
        "latitude": NAPA_LAT,
        "longitude": NAPA_LON,
        "distance": DISTANCE_MILES,
        "format": "application/json",
        "API_KEY": api_key,
    }
    resp = httpx.get(
        f"{AIRNOW_BASE}/aq/observation/latLong/current/",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def store_observations(conn: sqlite3.Connection, records: list[dict]) -> int:
    if not records:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows = []

    for r in records:
        param = r.get("ParameterName", "")
        aqi = r.get("AQI")
        cat_num = r.get("Category", {}).get("Number")
        cat_name = r.get("Category", {}).get("Name", "").strip()

        rows.append({
            "collected_at": now,
            "obs_date": r.get("DateObserved", "").strip(),
            "obs_hour": r.get("HourObserved"),
            "reporting_area": r.get("ReportingArea", "").strip(),
            "state_code": r.get("StateCode", "").strip(),
            "latitude": r.get("Latitude"),
            "longitude": r.get("Longitude"),
            "parameter": param,
            "aqi": int(aqi) if aqi is not None else None,
            "category_number": int(cat_num) if cat_num is not None else None,
            "category_name": cat_name,
        })

        # Fire-relevant warning
        flag = ""
        if cat_num and int(cat_num) >= 3:
            flag = " ⚠️"
        elif param == "PM2.5" and aqi and int(aqi) > 50:
            flag = " 👀"

        log.info(
            "  %s | AQI: %s | %s%s",
            param,
            aqi,
            cat_name,
            flag,
        )

    conn.executemany(
        """
        INSERT INTO observations (
            collected_at, obs_date, obs_hour, reporting_area, state_code,
            latitude, longitude, parameter, aqi, category_number, category_name
        ) VALUES (
            :collected_at, :obs_date, :obs_hour, :reporting_area, :state_code,
            :latitude, :longitude, :parameter, :aqi, :category_number, :category_name
        )
        """,
        rows,
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll(conn: sqlite3.Connection) -> None:
    api_key = get_api_key()

    log.info("Fetching AQI observations for Napa County...")
    try:
        records = fetch_observations(api_key)
    except httpx.HTTPError as exc:
        log.error("AirNow fetch failed: %s", exc)
        return

    if not records:
        log.warning("AirNow returned empty response — this can happen occasionally, will retry next poll")
        return

    count = store_observations(conn, records)
    log.info("Stored %d observation(s)", count)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AQI data collector")
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
