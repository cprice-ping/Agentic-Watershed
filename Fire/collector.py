"""
Fire Collector
--------------
Polls NASA FIRMS (Fire Information for Resource Management System) for
satellite-detected thermal hotspots within a bounding box around Napa
Valley, and stores them into a local SQLite database.

Motivation: Synthesis's own cross-domain reasoning has repeatedly flagged
"the upwind fire source has not been identified or confirmed extinguished"
as an open uncertainty — Weather covers fire *weather* (wind/humidity/Red
Flag Warnings) and AQI covers the *smoke signature* (PM2.5 rising with a
flat ozone fingerprint), but nothing looked for an actual fire. This does.

Data source: NASA FIRMS Area API (VIIRS/MODIS near-real-time hotspot
detections). Free, no cost, requires a free MAP_KEY — register at
https://firms.modaps.eosdis.nasa.gov/api/map_key/ and set FIRMS_API_KEY.

Designed to run on a schedule (cron) or continuously with --loop.

Usage:
  python collector.py           # single poll
  python collector.py --loop    # poll every 30 minutes
  python collector.py --init    # initialise DB only
"""

import argparse
import csv
import io
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration  (location-specific values come from node_config.json)
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "fire.db"

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
FIRE_CFG = _NODE_CFG["fire"]
BBOX = FIRE_CFG["bbox"]                # "west,south,east,north"
# Each VIIRS satellite (SNPP, NOAA-20, NOAA-21) has its own ~12h-offset
# overpass schedule — polling only one source means missing whatever the
# other two satellites caught in between. "sources" (list) is preferred;
# "source" (single string) still works for backward compat.
SOURCES = FIRE_CFG.get("sources") or [FIRE_CFG["source"]]
DAY_RANGE = FIRE_CFG["day_range"]      # 1-10
HOME_LAT = FIRE_CFG["home_lat"]
HOME_LON = FIRE_CFG["home_lon"]

FIRMS_API_KEY = os.environ.get("FIRMS_API_KEY", "").strip()
FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

POLL_INTERVAL_SECONDS = 30 * 60  # 30 minutes — FIRMS NRT data updates a few times/day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fire.collector")


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
        CREATE TABLE IF NOT EXISTS hotspots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at    TEXT NOT NULL,          -- ISO8601 UTC, when we polled
            latitude        REAL NOT NULL,
            longitude       REAL NOT NULL,
            acq_date        TEXT NOT NULL,          -- satellite acquisition date
            acq_time        TEXT NOT NULL,          -- satellite acquisition time (HHMM UTC)
            satellite       TEXT,
            confidence      TEXT,                   -- VIIRS: l/n/h ; MODIS: 0-100
            frp             REAL,                   -- Fire Radiative Power (MW) — intensity proxy
            daynight        TEXT,
            distance_mi     REAL,                   -- great-circle distance from home_lat/home_lon
            UNIQUE(latitude, longitude, acq_date, acq_time, satellite)
        );

        CREATE INDEX IF NOT EXISTS idx_hotspots_time
            ON hotspots (collected_at DESC);

        CREATE TABLE IF NOT EXISTS agent_observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at     TEXT NOT NULL,
            summary         TEXT NOT NULL,
            flagged         INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
            reasoning       TEXT,
            raw_context     TEXT                         -- JSON snapshot agent used
        );

        -- One row per collector run, written unconditionally (success or
        -- failure). hotspots.collected_at freezes at first-seen time for a
        -- given hotspot (INSERT OR IGNORE dedup), so it can't answer "did we
        -- poll recently" once a hotspot goes quiet — this table can.
        CREATE TABLE IF NOT EXISTS polls (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at           TEXT NOT NULL,          -- ISO8601 UTC
            status              TEXT NOT NULL,          -- 'ok' or 'error'
            hotspots_fetched    INTEGER,                -- rows in the CSV response
            hotspots_new        INTEGER,                -- rows actually inserted
            error_message       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_polls_time
            ON polls (polled_at DESC);
    """)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    r_mi = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r_mi * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# FIRMS fetch
# ---------------------------------------------------------------------------

def fetch_firms(source: str) -> str:
    """Call the FIRMS Area API for a single source and return the raw CSV text."""
    if not FIRMS_API_KEY:
        raise RuntimeError("FIRMS_API_KEY is not set — register at "
                           "https://firms.modaps.eosdis.nasa.gov/api/map_key/")
    url = f"{FIRMS_BASE_URL}/{FIRMS_API_KEY}/{source}/{BBOX}/{DAY_RANGE}"
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_firms_csv(text: str) -> list[dict]:
    """
    Parse the FIRMS Area API CSV into a flat list of hotspot dicts.
    Expected columns (VIIRS NRT): latitude,longitude,bright_ti4,scan,track,
    acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (KeyError, ValueError):
            continue

        frp_raw = r.get("frp")
        try:
            frp = float(frp_raw) if frp_raw not in (None, "") else None
        except ValueError:
            frp = None

        rows.append({
            "collected_at": now,
            "latitude": lat,
            "longitude": lon,
            "acq_date": r.get("acq_date", ""),
            "acq_time": r.get("acq_time", ""),
            "satellite": r.get("satellite", ""),
            "confidence": r.get("confidence", ""),
            "frp": frp,
            "daynight": r.get("daynight", ""),
            "distance_mi": round(haversine_mi(HOME_LAT, HOME_LON, lat, lon), 1),
        })
    return rows


def store_hotspots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO hotspots
            (collected_at, latitude, longitude, acq_date, acq_time,
             satellite, confidence, frp, daynight, distance_mi)
        VALUES
            (:collected_at, :latitude, :longitude, :acq_date, :acq_time,
             :satellite, :confidence, :frp, :daynight, :distance_mi)
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


def record_poll(conn: sqlite3.Connection, status: str, hotspots_fetched: int = None,
                 hotspots_new: int = None, error_message: str = None) -> None:
    """Log the outcome of a collector run, independent of whether any
    hotspot rows were new — this is what "did the collector run" means,
    as distinct from "did anything change"."""
    conn.execute(
        """
        INSERT INTO polls (polled_at, status, hotspots_fetched, hotspots_new, error_message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(), status, hotspots_fetched, hotspots_new, error_message),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll(conn: sqlite3.Connection) -> None:
    log.info("Polling FIRMS (%s) for bbox %s, last %d day(s)...", ", ".join(SOURCES), BBOX, DAY_RANGE)

    all_rows = []
    failed_sources = []
    for source in SOURCES:
        try:
            text = fetch_firms(source)
        except (httpx.HTTPError, RuntimeError) as exc:
            log.error("FIRMS fetch failed for %s: %s", source, exc)
            failed_sources.append(f"{source}: {exc}")
            continue
        source_rows = parse_firms_csv(text)
        log.info("  %s: fetched %d hotspot(s) in window", source, len(source_rows))
        all_rows.extend(source_rows)

    if failed_sources and len(failed_sources) == len(SOURCES):
        # Every source failed — this is a genuine collector failure, not a
        # quiet poll. Distinct from a partial failure (some sources ok).
        record_poll(conn, status="error", error_message="; ".join(failed_sources))
        return

    new_count = store_hotspots(conn, all_rows)
    log.info("Fetched %d hotspot(s) total across %d source(s), %d new (deduped by lat/lon/acq time/satellite)",
             len(all_rows), len(SOURCES) - len(failed_sources), new_count)
    record_poll(
        conn, status="ok", hotspots_fetched=len(all_rows), hotspots_new=new_count,
        error_message="; ".join(failed_sources) if failed_sources else None,
    )

    nearby = sorted((r for r in all_rows if r["distance_mi"] <= 50), key=lambda r: r["distance_mi"])
    for r in nearby[:5]:
        log.info("  %.1fmi away | %s | confidence=%s | frp=%s MW | %s %s",
                  r["distance_mi"], r["satellite"], r["confidence"], r["frp"], r["acq_date"], r["acq_time"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fire hotspot collector (NASA FIRMS)")
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
