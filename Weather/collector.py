"""
Weather Collector
-----------------
Polls the NWS (National Weather Service) API for:
  - Hourly observations from Napa County Airport (KAPC)
  - Active weather alerts for Napa County

No API key required. NWS asks for a User-Agent header identifying your app.

Usage:
  python collector.py           # single poll
  python collector.py --loop    # poll every 30 minutes
  python collector.py --init    # initialise DB only
"""

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "data" / "weather.db"

# NWS observation station — Napa County Airport
# https://api.weather.gov/stations/KAPC/observations
OBSERVATION_STATION = "KAPC"

# NWS alerts zone for Napa County
# CAZ505 = Napa County interior valleys (fire weather zone)
# CAC055 = Napa County (general)
ALERT_ZONES = ["CAZ505", "CAC055"]

NWS_BASE = "https://api.weather.gov"

# NWS requires a User-Agent — identify your app and contact
USER_AGENT = "watershed-monitor/1.0 (napa-river-project)"

POLL_INTERVAL_SECONDS = 30 * 60  # 30 minutes (NWS updates hourly)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("weather.collector")


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
            station_id          TEXT NOT NULL,
            station_name        TEXT,
            obs_time            TEXT,               -- NWS observation timestamp
            temperature_c       REAL,
            temperature_f       REAL,
            humidity_pct        REAL,
            wind_speed_kmh      REAL,
            wind_speed_mph      REAL,
            wind_direction_deg  REAL,
            wind_gust_kmh       REAL,
            wind_gust_mph       REAL,
            precip_1h_mm        REAL,
            precip_6h_mm        REAL,
            precip_24h_mm       REAL,
            visibility_m        REAL,
            text_description    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_obs_time
            ON observations (collected_at DESC);

        CREATE TABLE IF NOT EXISTS alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at    TEXT NOT NULL,
            alert_id        TEXT NOT NULL,
            event           TEXT,               -- e.g. "Red Flag Warning"
            severity        TEXT,               -- Extreme/Severe/Moderate/Minor
            urgency         TEXT,
            headline        TEXT,
            description     TEXT,
            onset           TEXT,
            expires         TEXT,
            zones           TEXT                -- comma-separated zone list
        );

        CREATE INDEX IF NOT EXISTS idx_alerts_time
            ON alerts (collected_at DESC);

        CREATE TABLE IF NOT EXISTS agent_observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at     TEXT NOT NULL,
            summary         TEXT NOT NULL,
            flagged         INTEGER NOT NULL DEFAULT 0,
            reasoning       TEXT,
            raw_context     TEXT
        );
    """)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# NWS fetch helpers
# ---------------------------------------------------------------------------

def nws_get(client: httpx.Client, path: str) -> dict:
    url = f"{NWS_BASE}{path}"
    resp = client.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def celsius_to_fahrenheit(c: float | None) -> float | None:
    return round(c * 9 / 5 + 32, 1) if c is not None else None


def kmh_to_mph(kmh: float | None) -> float | None:
    return round(kmh * 0.621371, 1) if kmh is not None else None


def ms_to_kmh(ms: float | None) -> float | None:
    return round(ms * 3.6, 1) if ms is not None else None


def extract_value(prop: dict | None) -> float | None:
    """Extract numeric value from NWS QuantitativeValue object."""
    if prop is None:
        return None
    v = prop.get("value")
    return float(v) if v is not None else None


# ---------------------------------------------------------------------------
# Fetch observations
# ---------------------------------------------------------------------------

def fetch_observations(client: httpx.Client, conn: sqlite3.Connection) -> int:
    log.info("Fetching observations from KAPC...")
    data = nws_get(client, f"/stations/{OBSERVATION_STATION}/observations?limit=1")

    features = data.get("features", [])
    if not features:
        log.warning("No observation features returned")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows = []

    for feature in features:
        props = feature.get("properties", {})

        temp_c = extract_value(props.get("temperature"))
        wind_ms = extract_value(props.get("windSpeed"))
        wind_kmh = ms_to_kmh(wind_ms)
        gust_ms = extract_value(props.get("windGust"))
        gust_kmh = ms_to_kmh(gust_ms)

        rows.append({
            "collected_at": now,
            "station_id": OBSERVATION_STATION,
            "station_name": props.get("station", "").split("/")[-1],
            "obs_time": props.get("timestamp"),
            "temperature_c": temp_c,
            "temperature_f": celsius_to_fahrenheit(temp_c),
            "humidity_pct": extract_value(props.get("relativeHumidity")),
            "wind_speed_kmh": wind_kmh,
            "wind_speed_mph": kmh_to_mph(wind_kmh),
            "wind_direction_deg": extract_value(props.get("windDirection")),
            "wind_gust_kmh": gust_kmh,
            "wind_gust_mph": kmh_to_mph(gust_kmh),
            "precip_1h_mm": extract_value(props.get("precipitationLastHour")),
            "precip_6h_mm": extract_value(props.get("precipitationLast6Hours")),
            "precip_24h_mm": extract_value(props.get("precipitationLast24Hours")),
            "visibility_m": extract_value(props.get("visibility")),
            "text_description": props.get("textDescription"),
        })

        log.info(
            "  %s | %.1f°F | Humidity: %s%% | Wind: %s mph @ %s° | Gusts: %s mph",
            props.get("textDescription", "—"),
            celsius_to_fahrenheit(temp_c) or 0,
            round(extract_value(props.get("relativeHumidity")) or 0),
            kmh_to_mph(wind_kmh) or "—",
            round(extract_value(props.get("windDirection")) or 0),
            kmh_to_mph(gust_kmh) or "—",
        )

    conn.executemany(
        """
        INSERT INTO observations (
            collected_at, station_id, station_name, obs_time,
            temperature_c, temperature_f, humidity_pct,
            wind_speed_kmh, wind_speed_mph, wind_direction_deg,
            wind_gust_kmh, wind_gust_mph,
            precip_1h_mm, precip_6h_mm, precip_24h_mm,
            visibility_m, text_description
        ) VALUES (
            :collected_at, :station_id, :station_name, :obs_time,
            :temperature_c, :temperature_f, :humidity_pct,
            :wind_speed_kmh, :wind_speed_mph, :wind_direction_deg,
            :wind_gust_kmh, :wind_gust_mph,
            :precip_1h_mm, :precip_6h_mm, :precip_24h_mm,
            :visibility_m, :text_description
        )
        """,
        rows,
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Fetch alerts
# ---------------------------------------------------------------------------

def fetch_alerts(client: httpx.Client, conn: sqlite3.Connection) -> int:
    log.info("Fetching active alerts for Napa County zones...")
    zone_str = ",".join(ALERT_ZONES)
    data = nws_get(client, f"/alerts/active?zone={zone_str}")

    features = data.get("features", [])
    now = datetime.now(timezone.utc).isoformat()

    if not features:
        log.info("  No active alerts")
        return 0

    rows = []
    for feature in features:
        props = feature.get("properties", {})
        affected = props.get("affectedZones", [])
        zones = ",".join(z.split("/")[-1] for z in affected)

        rows.append({
            "collected_at": now,
            "alert_id": props.get("id", ""),
            "event": props.get("event"),
            "severity": props.get("severity"),
            "urgency": props.get("urgency"),
            "headline": props.get("headline"),
            "description": (props.get("description") or "")[:2000],  # cap length
            "onset": props.get("onset"),
            "expires": props.get("expires"),
            "zones": zones,
        })
        log.info("  ⚠️  %s (%s) — expires %s", props.get("event"), props.get("severity"), props.get("expires"))

    conn.executemany(
        """
        INSERT INTO alerts (
            collected_at, alert_id, event, severity, urgency,
            headline, description, onset, expires, zones
        ) VALUES (
            :collected_at, :alert_id, :event, :severity, :urgency,
            :headline, :description, :onset, :expires, :zones
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
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json",
    }
    with httpx.Client(headers=headers) as client:
        try:
            obs_count = fetch_observations(client, conn)
            log.info("Stored %d observation(s)", obs_count)
        except httpx.HTTPError as exc:
            log.error("Observation fetch failed: %s", exc)

        try:
            alert_count = fetch_alerts(client, conn)
            log.info("Stored %d alert(s)", alert_count)
        except httpx.HTTPError as exc:
            log.error("Alert fetch failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Weather data collector")
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
