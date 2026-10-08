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
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration  (location-specific values come from node_config.json)
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "aqi.db"

AIRNOW_BASE = "https://www.airnowapi.org"

_NODE_CFG    = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
LOCATION_LAT = _NODE_CFG["aqi"]["lat"]
LOCATION_LON = _NODE_CFG["aqi"]["lon"]

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

# httpx logs every request URL at INFO. AirNow takes its key as a query parameter, so
# that line printed the key on every poll — into the Pi's log files for
# months, then into `docker compose logs`, and on 2026-10-08 into a chat
# while debugging. WARNING keeps httpx's real problems and drops the URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)

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
            category_name       TEXT,
            site_id             TEXT,               -- monitor the reading came from
            site_name           TEXT,
            reporting_agency    TEXT,
            aqi_kind            TEXT,               -- 'nowcast'; NULL = not recorded
            local_tz            TEXT                -- AirNow's zone for obs_hour
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
            reasoning       TEXT,
            model           TEXT,                        -- model id that produced it
            rules_flagged   INTEGER,                     -- deterministic verdict (shadow)
            rules_fired     TEXT,                        -- JSON list of rules that matched
            input_tokens    INTEGER,                     -- from response.usage
            output_tokens   INTEGER
        );
    """)
    _add_missing_columns(conn)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# Columns added when AirNow's observation service changed (2026-10-08). Added
# in place, so a node's existing history survives; rows from before the change
# keep NULL in them, which reads as "not recorded", not as any value.
_ADDED_COLUMNS = {
    "site_id": "TEXT",
    "site_name": "TEXT",
    "reporting_agency": "TEXT",
    "aqi_kind": "TEXT",
    "local_tz": "TEXT",
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    for name, kind in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {kind}")
            log.info("Added observations.%s", name)


# ---------------------------------------------------------------------------
# AirNow fetch
# ---------------------------------------------------------------------------

KEPT_PARAMETERS = ("PM2.5", "OZONE")

_CATEGORY_BY_NAME = {name.lower(): num for num, name in AQI_CATEGORIES.items()}

# EPA AQI category upper bounds, for when a name is missing or unrecognised.
_CATEGORY_UPPER = ((50, 1), (100, 2), (150, 3), (200, 4), (300, 5))


def _category_number(name: str, aqi) -> int | None:
    """The EPA category number, which the flag rules read.

    The new service sends only the name. Taken from the name when it is one
    of the six EPA names; otherwise from the AQI value's band, logged, since
    an unrecognised name means the reply changed again.
    """
    num = _CATEGORY_BY_NAME.get((name or "").lower())
    if num is not None:
        return num
    if aqi is None:
        return None
    log.warning("Unrecognised AirNow category name %r; using the AQI band", name)
    value = int(aqi)
    for upper, cat in _CATEGORY_UPPER:
        if value <= upper:
            return cat
    return 6


def _hour(value) -> int | None:
    """hourObserved: "09:00" now, an integer before. Stored as the integer
    hour, local time, matching existing rows."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).split(":")[0])
    except ValueError:
        return None

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

    /aq/observation/current/ziplatlong/ replaced /aq/observation/latLong/
    current/, which AirNow retired on 2026-09-30 and which now answers 410.
    What changed besides the path, from the first live response (2026-10-08):

      - Each reading names the single monitor it came from (siteName,
        siteID). The old service gave one value per pollutant for the
        reporting area. The nearest monitor is chosen per pollutant, so
        PM2.5 and ozone can come from different places (Vallejo and
        Fairfield in that first response).
      - The search reached 50 miles ("lookupBoundary") although `distance`
        asked for 25. Kept in the request in case it is honoured later.
      - AQI arrives as nowcastAQI, and the category only as a name.
      - hourObserved is "HH:MM" text with localTimeZone beside it.
    """
    params = {
        "latitude": LOCATION_LAT,
        "longitude": LOCATION_LON,
        "distance": DISTANCE_MILES,
        "format": "application/json",
        "API_KEY": api_key,
    }
    resp = httpx.get(
        f"{AIRNOW_BASE}/aq/observation/current/ziplatlong/",
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
        param = (r.get("parameterName") or "").strip()
        # PM2.5 and ozone only, as before. The new service also returns the
        # nearest PM10 monitor — Downtown Sacramento, about 50 miles away, in
        # the first response — which says nothing about Napa's air.
        if param not in KEPT_PARAMETERS:
            continue
        aqi = r.get("nowcastAQI")
        cat_name = (r.get("aqiCategoryName") or "").strip()
        cat_num = _category_number(cat_name, aqi)

        rows.append({
            "collected_at": now,
            "obs_date": (r.get("dateObserved") or "").strip(),
            "obs_hour": _hour(r.get("hourObserved")),
            "reporting_area": (r.get("reportingAreaName") or "").strip(),
            "state_code": "",           # not in the new service's reply
            "latitude": None,           # nor are coordinates
            "longitude": None,
            "parameter": param,
            "aqi": int(aqi) if aqi is not None else None,
            "category_number": cat_num,
            "category_name": cat_name,
            "site_id": (r.get("siteID") or "").strip() or None,
            "site_name": (r.get("siteName") or "").strip() or None,
            "reporting_agency": (r.get("reportingAgency") or "").strip() or None,
            "aqi_kind": "nowcast",
            "local_tz": (r.get("localTimeZone") or "").strip() or None,
        })

        # Fire-relevant warning
        flag = ""
        if cat_num and int(cat_num) >= 3:
            flag = " ⚠️"
        elif param == "PM2.5" and aqi and int(aqi) > 50:
            flag = " 👀"

        log.info(
            "  %s | AQI: %s | %s | %s%s",
            param,
            aqi,
            cat_name,
            r.get("siteName") or "?",
            flag,
        )

    conn.executemany(
        """
        INSERT INTO observations (
            collected_at, obs_date, obs_hour, reporting_area, state_code,
            latitude, longitude, parameter, aqi, category_number, category_name,
            site_id, site_name, reporting_agency, aqi_kind, local_tz
        ) VALUES (
            :collected_at, :obs_date, :obs_hour, :reporting_area, :state_code,
            :latitude, :longitude, :parameter, :aqi, :category_number, :category_name,
            :site_id, :site_name, :reporting_agency, :aqi_kind, :local_tz
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
        # httpx's error text includes the full URL, key and all.
        log.error("AirNow fetch failed: %s",
                  str(exc).replace(api_key, "<AIRNOW_API_KEY>"))
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
