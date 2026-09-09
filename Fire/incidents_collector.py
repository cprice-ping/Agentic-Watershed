"""
CAL FIRE Incident Collector
---------------------------
Polls CAL FIRE's published incident list into the same fire.db the FIRMS
hotspot collector writes to, so the MCP server can match a satellite thermal
detection against a named, human-confirmed incident.

Why this exists. FIRMS detects thermal anomalies, and a thermal anomaly has no
type: a wildfire, a prescribed burn, an agricultural burn and a refinery flare
are identical in that data. On 2026-09-08 a 66 MW detection drove Synthesis to
extreme fire risk for Napa Valley; it was a controlled burn on Angel Island in
San Francisco Bay, across open water with no fuel path to the valley. Nothing
in the pipeline could have distinguished it.

WHAT A MATCH MEANS, AND WHAT IT DOES NOT. Matching is a POSITIVE identifier
only. A match tells you a detection is a known incident and gives you its
name, acreage and containment. An absence tells you nothing — not that the
detection is harmless, not that it isn't a fire. Two independent gaps make
that so.

Publication lags ignition. An incident has to be reported, confirmed and
published before it appears; a satellite sees the heat immediately. There is
always a window in which a real fire is detected and not yet listed.

And the feed is a curated subset, not a census. The full 2026 list held 484
incidents statewide, of which 4 were active — far fewer than the fires
California actually has in a year. A small fire local crews handle may never
get an incident page at all: on 2026-09-09 a fire near Willits was visible on
Watch Duty and absent from this feed entirely, active and inactive alike. The
nearest Willits-area record was the Ponderosa Fire, 5 acres, from nearly a
month earlier. Types observed across that full year were Wildfire (472), Fire
(11) and Hazmat (1) — there is no prescribed-burn category, so a controlled
burn will never match.

Anything built on top of this must not read "unmatched" as "safe".

Endpoint: https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List
  ?inactive=true   every incident the feed carries, closed ones included
  ?inactive=false  only those currently active

This polls inactive=true, and that choice is what allows the low polling
frequency below. Incidents drop out of the active feed the moment they close,
so polling active-only would need to run often enough to catch a short-lived
fire inside its own lifetime — and would still silently miss anything that
closed before the collector's first run. The Willits fire on 2026-09-09 was
absent from the active feed for exactly this reason. Fetching everything makes
each poll a complete picture instead of a sample, so the cadence can match
consumption rather than chase closures.

Rows are kept forever and upserted on CAL FIRE's UniqueId regardless, since a
hotspot inside the 72-hour currency window must stay matchable against an
incident that has since gone inactive. Retention also matters across the year
boundary: every record in the 2026 fetch carried a 2026 start date, so the
feed appears to be current-year only and this table is the sole place last
year's incidents will survive.

Usage:
  python incidents_collector.py           # single poll
  python incidents_collector.py --init    # create the table and exit
  python incidents_collector.py --dry-run # fetch and print, write nothing

Cron. Fire records are built twice a day — the Fire agent runs at 3 and 15 and
the publisher picks those up at 3:15 and 15:15 — so incident data only needs to
be current at two moments. Polling the full feed means each run is complete, so
there is nothing to gain from running it more often:
  50 2,14 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python incidents_collector.py >> logs/incidents.log 2>&1
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import thresholds  # noqa: E402
from collector import haversine_mi  # noqa: E402

DB_PATH = Path(__file__).parent / "data" / "fire.db"

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
HOME_LAT = _NODE_CFG["fire"]["home_lat"]
HOME_LON = _NODE_CFG["fire"]["home_lon"]

INCIDENTS_URL = "https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List"
USER_AGENT = "watershed-monitor/1.0 (napa-river-project)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fire.incidents")


# CAL FIRE field name to column. Kept in one place because it is the part most
# likely to break: this is a CMS endpoint, not a versioned API, and the field
# names are whatever Umbraco happens to serialise. Every row also stores its
# raw JSON, so a wrong mapping here can be corrected later without re-polling
# history that the active feed will no longer serve.
_FIELD_MAP = {
    "UniqueId":         "unique_id",
    "Name":             "name",
    "County":           "county",
    "Location":         "location",
    "Latitude":         "latitude",
    "Longitude":        "longitude",
    "AcresBurned":      "acres_burned",
    "PercentContained": "percent_contained",
    "Type":             "incident_type",
    "IsActive":         "is_active",
    "Started":          "started_at",
    "Updated":          "updated_at",
    "ExtinguishedDate": "extinguished_at",
    "Url":              "url",
}


def get_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the incidents table alongside the hotspots table.

    Lives in fire.db rather than its own file so the MCP server can match
    hotspots to incidents in one query instead of attaching a second database.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            unique_id         TEXT PRIMARY KEY,   -- CAL FIRE UniqueId (uuid)
            first_seen_at     TEXT NOT NULL,      -- when we first polled it
            last_seen_at      TEXT NOT NULL,      -- when it was last in the feed
            name              TEXT,
            county            TEXT,
            location          TEXT,
            latitude          REAL,
            longitude         REAL,
            acres_burned      REAL,
            percent_contained REAL,
            incident_type     TEXT,               -- CAL FIRE's Type, e.g. Wildfire
            is_active         INTEGER,
            started_at        TEXT,
            updated_at        TEXT,
            extinguished_at   TEXT,
            url               TEXT,
            distance_mi       REAL,               -- from home, precomputed
            raw_json          TEXT NOT NULL       -- the record as received
        );

        CREATE INDEX IF NOT EXISTS idx_incidents_distance
            ON incidents (distance_mi ASC);
        CREATE INDEX IF NOT EXISTS idx_incidents_active
            ON incidents (is_active, last_seen_at DESC);
    """)
    conn.commit()


def _clean(value):
    """CAL FIRE pads some names with trailing spaces ("Timber Fire ")."""
    return value.strip() if isinstance(value, str) else value


def parse_incidents(payload) -> list[dict]:
    """Map the API payload to rows. Tolerant by design.

    An incident missing coordinates is kept rather than dropped — it still has
    a name, a county and a containment figure, and dropping it silently is the
    failure mode this whole project keeps rediscovering. It simply gets no
    distance and cannot be matched to a hotspot.
    """
    items = payload if isinstance(payload, list) else (
        next((v for v in payload.values() if isinstance(v, list)), []))
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {col: _clean(item.get(api_field))
               for api_field, col in _FIELD_MAP.items()}
        if not row.get("unique_id"):
            log.warning("Incident with no UniqueId, skipping: %s",
                        str(item)[:120])
            continue
        row["is_active"] = int(bool(row.get("is_active")))
        try:
            row["distance_mi"] = round(haversine_mi(
                HOME_LAT, HOME_LON,
                float(row["latitude"]), float(row["longitude"])), 2)
        except (TypeError, ValueError):
            row["distance_mi"] = None
        row["first_seen_at"] = now
        row["last_seen_at"] = now
        row["raw_json"] = json.dumps(item, separators=(",", ":"))
        rows.append(row)
    return rows


def store(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """Upsert on UniqueId. Returns (new, updated).

    first_seen_at is preserved across updates; everything else takes the
    latest value, because acreage and containment move as a fire progresses
    and the newest figure is the one worth reporting.
    """
    existing = {r[0] for r in conn.execute("SELECT unique_id FROM incidents")}
    new = sum(1 for r in rows if r["unique_id"] not in existing)
    cols = list(rows[0].keys()) if rows else []
    if rows:
        assignments = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c != "first_seen_at")
        conn.executemany(
            f"""INSERT INTO incidents ({', '.join(cols)})
                VALUES ({', '.join(':' + c for c in cols)})
                ON CONFLICT(unique_id) DO UPDATE SET {assignments}""",
            rows,
        )
        conn.commit()
    return new, len(rows) - new


def poll(conn: sqlite3.Connection, dry_run: bool = False,
         active_only: bool = False) -> int:
    log.info("Fetching CAL FIRE incidents (%s)...",
             "active only" if active_only else "including closed")
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(INCIDENTS_URL,
                          params={"inactive": "false" if active_only else "true"})
        resp.raise_for_status()
        payload = resp.json()

    rows = parse_incidents(payload)
    active = sum(1 for r in rows if r["is_active"])
    log.info("%d incident(s) statewide, %d currently active", len(rows), active)

    located = [r for r in rows if r["distance_mi"] is not None]
    for r in sorted(located, key=lambda r: r["distance_mi"])[:5]:
        log.info("  %6.1f mi  %-28s %s acres, %s%% contained",
                 r["distance_mi"], str(r["name"])[:28],
                 r["acres_burned"], r["percent_contained"])
    if len(located) < len(rows):
        log.warning("%d incident(s) have no usable coordinates and cannot be "
                    "matched to a hotspot", len(rows) - len(located))

    if dry_run:
        log.info("[DRY RUN] nothing written")
        return len(rows)

    new, updated = store(conn, rows)
    log.info("Stored: %d new, %d updated", new, updated)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="CAL FIRE incident collector")
    parser.add_argument("--init", action="store_true", help="Create table and exit")
    parser.add_argument("--dry-run", action="store_true", help="Fetch, write nothing")
    parser.add_argument("--active-only", action="store_true",
                        help="Fetch only currently-active incidents. Faster, but "
                             "misses anything that has already closed — see the "
                             "module docstring before using it.")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB")
    args = parser.parse_args()

    conn = get_db(Path(args.db))
    init_db(conn)
    if args.init:
        log.info("Incidents table ready at %s", args.db)
        return
    poll(conn, dry_run=args.dry_run, active_only=args.active_only)


if __name__ == "__main__":
    main()
