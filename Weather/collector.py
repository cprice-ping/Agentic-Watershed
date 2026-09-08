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

DB_PATH = Path(__file__).parent / "data" / "weather.db"

_NODE_CFG           = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
OBSERVATION_STATION = _NODE_CFG["weather"]["observation_station"]
ALERT_ZONES         = _NODE_CFG["weather"]["alert_zones"]

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
            raw_context     TEXT,
            model           TEXT,                        -- model id that produced it
            rules_flagged   INTEGER,                     -- deterministic verdict (shadow)
            rules_fired     TEXT,                        -- JSON list of rules that matched
            input_tokens    INTEGER,                     -- from response.usage
            output_tokens   INTEGER
        );
    """)
    conn.commit()
    _migrate(conn)
    log.info("Database initialised at %s", DB_PATH)


# The factor by which every wind row collected before the unitCode fix was
# inflated: the collector read km/h, called it m/s, and multiplied by 3.6.
_WIND_INFLATION = 3.6


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent data migrations, run from init_db on every startup.

    Currently one: divide the historical wind columns by 3.6.

    Correcting in place rather than marking a cutover, because this is not an
    estimate. The stored value is the true reading times a known constant, so
    the correction is exact and reversible. Leaving the rows would keep the
    48-hour trend window mixing real and inflated numbers for two days after
    deploy, and would leave a permanently unusable month of history behind
    that.

    Guarded by schema_migrations, because running it twice would divide by
    12.96 and there is no marker in a row itself to tell corrected from not.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            note       TEXT
        )
    """)
    conn.commit()

    name = "2026-09-08-wind-kmh-mistaken-for-ms"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?",
                    (name,)).fetchone():
        return

    affected = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE wind_speed_kmh IS NOT NULL"
        "   OR wind_gust_kmh IS NOT NULL OR wind_speed_mph IS NOT NULL"
        "   OR wind_gust_mph IS NOT NULL"
    ).fetchone()[0]

    conn.execute(
        """
        UPDATE observations SET
            wind_speed_kmh = ROUND(wind_speed_kmh / ?, 1),
            wind_speed_mph = ROUND(wind_speed_mph / ?, 1),
            wind_gust_kmh  = ROUND(wind_gust_kmh  / ?, 1),
            wind_gust_mph  = ROUND(wind_gust_mph  / ?, 1)
        """,
        (_WIND_INFLATION,) * 4,
    )
    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at, note) VALUES (?, ?, ?)",
        (name, datetime.now(timezone.utc).isoformat(),
         f"divided {affected} rows' wind columns by {_WIND_INFLATION}"),
    )
    conn.commit()
    log.warning(
        "Corrected %d historical observation rows: wind was stored %.1fx too "
        "high because km/h was read as m/s. Agent observations and published "
        "ATProto records from before this point still quote the inflated "
        "figures and are not rewritten.", affected, _WIND_INFLATION)


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


# NWS QuantitativeValue objects declare their unit in unitCode, and the units
# are not stable across endpoints or over time. Wind on the observations
# endpoint is km/h (wmoUnit:km_h-1); this collector assumed m/s and converted
# m/s → km/h → mph, multiplying every wind value by 3.6.
#
# That is not a small error. It recorded routine Napa breezes as 70-95 mph
# gusts, which crossed the 45 mph gust flag rule on almost every run — the
# Weather agent's constant flagging, which had been read as model drift or a
# badly chosen threshold, was neither. The threshold was fine and the model
# was reasoning correctly about numbers that were wrong before it saw them.
#
# Nothing downstream could catch it: flag_rules.py reads the same column, so
# the shadow verdict agreed with the model while both were wrong, and
# Synthesis resolved fire predictions against the published windGustMph.
#
# So: convert from what the payload declares, never from what we expect. An
# unrecognised unit yields None and a warning rather than a guess — a missing
# reading is recoverable, a plausible-looking wrong one is not.
_TO_KMH = {
    "wmoUnit:km_h-1": 1.0,
    "wmoUnit:m_s-1":  3.6,
    "unit:m_s-1":     3.6,        # pre-2021 NWS spelling
    "wmoUnit:mi_h-1": 1.609344,
    "unit:mi_h-1":    1.609344,
}

_TO_DEGC = {
    "wmoUnit:degC": lambda v: v,
    "unit:degC":    lambda v: v,
    "wmoUnit:degF": lambda v: (v - 32) * 5 / 9,
    "unit:degF":    lambda v: (v - 32) * 5 / 9,
}


def extract_value(prop: dict | None) -> float | None:
    """Numeric value from an NWS QuantitativeValue, with no conversion.

    Only for quantities whose unit this collector stores as-is — percent,
    millimetres, metres, degrees of bearing. Anything needing a conversion
    must go through a unit-aware helper below, so the unit comes from the
    payload rather than from an assumption.
    """
    if prop is None:
        return None
    v = prop.get("value")
    return float(v) if v is not None else None


def _converted(prop: dict | None, table: dict, quantity: str) -> float | None:
    """Value converted to the canonical unit using the payload's unitCode."""
    if prop is None:
        return None
    v = prop.get("value")
    if v is None:
        return None
    code = prop.get("unitCode")
    conv = table.get(code)
    if conv is None:
        log.warning(
            "Unrecognised unitCode %r for %s — dropping the reading rather "
            "than assuming a unit. Add it to the conversion table.", code, quantity)
        return None
    return float(v) * conv if isinstance(conv, float) else float(conv(float(v)))


def wind_kmh(prop: dict | None) -> float | None:
    """Wind speed in km/h, whatever unit NWS declared."""
    v = _converted(prop, _TO_KMH, "wind speed")
    return round(v, 1) if v is not None else None


def temperature_c(prop: dict | None) -> float | None:
    """Temperature in Celsius, whatever unit NWS declared."""
    v = _converted(prop, _TO_DEGC, "temperature")
    return round(v, 1) if v is not None else None


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

        temp_c = temperature_c(props.get("temperature"))
        wind_speed_kmh = wind_kmh(props.get("windSpeed"))
        gust_kmh = wind_kmh(props.get("windGust"))

        rows.append({
            "collected_at": now,
            "station_id": OBSERVATION_STATION,
            "station_name": props.get("station", "").split("/")[-1],
            "obs_time": props.get("timestamp"),
            "temperature_c": temp_c,
            "temperature_f": celsius_to_fahrenheit(temp_c),
            "humidity_pct": extract_value(props.get("relativeHumidity")),
            "wind_speed_kmh": wind_speed_kmh,
            "wind_speed_mph": kmh_to_mph(wind_speed_kmh),
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
            kmh_to_mph(wind_speed_kmh) or "—",
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
