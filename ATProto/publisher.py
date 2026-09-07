"""
ATProto Node Publisher
----------------------
Publishes domain agent observations (watershed, weather, aqi) as structured
ATProto lexicon records, under the node's own identity.

Domain observations are machine-readable records intended for other agents
(Synthesis) to consume, not a human-facing feed — there is no accompanying
app.bsky.feed.post here. That's the synthesis publisher's job, on its own
identity, on the public Bluesky network.

PDS: defaults to a self-hosted PDS (see ATProto/pds/) so the node's identity
     doesn't depend on Bluesky-run infrastructure. Override with
     ATPROTO_PDS_URL or "pds_url" in node_config.json; falls back to
     bsky.social if neither is set.

Identity: BSKY_HANDLE / BSKY_APP_PASSWORD → node DID (napa-node-01)
          Set in /etc/environment on the Pi. Against a self-hosted PDS these
          are the node's PDS account handle/password, not a Bluesky app
          password.

Usage:
  python publisher.py                     # publish any unpublished observations
  python publisher.py --dry-run           # show what would be published
  python publisher.py --domain watershed  # single domain

Cron (run after each agent cycle — 15 min after the last agent fires):
  15 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/ATProto && .venv/bin/python publisher.py >> logs/publisher.log 2>&1
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = Path(__file__).parent.parent

DB_PATHS = {
    "watershed": BASE / "River"   / "data" / "watershed.db",
    "weather":   BASE / "Weather" / "data" / "weather.db",
    "aqi":       BASE / "AQI"     / "data" / "aqi.db",
    "fire":      BASE / "Fire"    / "data" / "fire.db",
}

# Track what's been published — simple SQLite alongside the publisher
PUBLISHER_DB = Path(__file__).parent / "data" / "publisher.db"

LEXICON = "net.cpricedomain.temp.monitor.observation"

_NODE_CFG = json.loads((BASE / "node_config.json").read_text())
NODE_ID   = _NODE_CFG["node_id"]

# PDS endpoint domain agents publish to. Defaults to a self-hosted PDS
# (see ATProto/pds/) — set ATPROTO_PDS_URL or "pds_url" in node_config.json
# to override. Falls back to bsky.social only for back-compat.
BSKY_PDS = (
    os.environ.get("ATPROTO_PDS_URL")
    or _NODE_CFG.get("pds_url")
    or "https://bsky.social"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("atproto.publisher")


# ---------------------------------------------------------------------------
# Publisher DB — tracks what's been published to avoid duplicates
# ---------------------------------------------------------------------------

def init_publisher_db() -> sqlite3.Connection:
    PUBLISHER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PUBLISHER_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS published (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            published_at    TEXT NOT NULL,
            domain          TEXT NOT NULL,
            source_id       INTEGER NOT NULL,   -- rowid from source DB
            observed_at     TEXT NOT NULL,
            at_uri          TEXT,               -- at://did/.../rkey
            flagged         INTEGER DEFAULT 0,
            UNIQUE(domain, source_id)
        );
    """)
    conn.commit()
    return conn


def already_published(conn: sqlite3.Connection, domain: str, source_id: int) -> bool:
    row = conn.execute(
        "SELECT id FROM published WHERE domain = ? AND source_id = ?",
        (domain, source_id),
    ).fetchone()
    return row is not None


def mark_published(conn: sqlite3.Connection, domain: str, source_id: int,
                   observed_at: str, at_uri: str, flagged: bool) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO published
            (published_at, domain, source_id, observed_at, at_uri, flagged)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(), domain, source_id,
         observed_at, at_uri, int(flagged)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ATProto session
# ---------------------------------------------------------------------------

class BlueskySession:
    def __init__(self, handle: str, app_password: str):
        self.handle = handle
        self.app_password = app_password
        self.did = None
        self.access_jwt = None
        self.client = httpx.Client(base_url=BSKY_PDS, timeout=30)

    def login(self) -> None:
        resp = self.client.post(
            "/xrpc/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": self.app_password},
        )
        resp.raise_for_status()
        data = resp.json()
        self.did = data["did"]
        self.access_jwt = data["accessJwt"]
        log.info("Logged in as %s (DID: %s)", self.handle, self.did)

    def create_record(self, collection: str, record: dict) -> str:
        """Create a record in the PDS. Returns the AT URI."""
        resp = self.client.post(
            "/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {self.access_jwt}"},
            json={
                "repo": self.did,
                "collection": collection,
                "record": record,
            },
        )
        resp.raise_for_status()
        return resp.json().get("uri", "")


# ---------------------------------------------------------------------------
# Collector data enrichment — fetch numeric readings closest to observed_at
# ---------------------------------------------------------------------------

def _fetch_weather_numerics(observed_at: str) -> dict:
    db_path = DB_PATHS["weather"]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff = _stale_cutoff(observed_at, READING_MAX_AGE_HOURS)
        row = conn.execute(
            """
            SELECT temperature_f, humidity_pct, wind_speed_mph,
                   wind_direction_deg, wind_gust_mph, precip_24h_mm
            FROM observations
            WHERE collected_at >= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            LIMIT 1
            """,
            (cutoff, observed_at),
        ).fetchone()
        conn.close()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def _fetch_watershed_numerics(observed_at: str) -> dict:
    """Aggregate the nearest current reading from every reporting station.

    The Napa watershed is gauged at two points with very different regimes:
    St Helena runs dry by late summer while Napa still carries flow. Both
    report the same USGS parameter codes, and an earlier version of this
    query selected (parameter_code, value) with no station column, keeping
    whichever row happened to sit closest in time. One station's reading was
    published as the whole watershed's and the other was silently dropped —
    on 2026-09-06 that produced a record asserting 0.0 cfs and 0.47 ft while
    its own summary read "Near Napa (11458000): 0.14 cfs at 2.09 ft".

    The lexicon has always declared the right shape for two gauges — min,
    mean and max across stations — so aggregate rather than pick. Stations
    are reported as those that actually contributed, not the configured
    list, so a gauge that goes dark shows up as an absent station instead of
    disappearing into an average.
    """
    db_path = DB_PATHS["watershed"]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff = _stale_cutoff(observed_at, READING_MAX_AGE_HOURS)
        rows = conn.execute(
            """
            SELECT station_id, parameter_code, value
            FROM readings
            WHERE parameter_code IN ('00060', '00065') AND value IS NOT NULL
              AND collected_at >= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            """,
            (cutoff, observed_at),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}

    # Rows arrive ordered by distance from observedAt, so the first row seen
    # for a (station, parameter) pair is that station's nearest reading.
    nearest: dict[tuple, float] = {}
    for r in rows:
        nearest.setdefault((r["station_id"], r["parameter_code"]), r["value"])

    discharge = [v for (_, code), v in nearest.items() if code == "00060"]
    gage      = [v for (_, code), v in nearest.items() if code == "00065"]

    result: dict = {}
    if discharge:
        result["dischargeMinCfs"]  = min(discharge)
        result["dischargeMeanCfs"] = round(sum(discharge) / len(discharge), 3)
        result["dischargeMaxCfs"]  = max(discharge)
    if gage:
        result["gageHeightMinFt"] = min(gage)
        result["gageHeightMaxFt"] = max(gage)
    if nearest:
        result["stationIds"] = sorted({sid for sid, _ in nearest})
    return result


# Seven-day trend thresholds. A watershed in a dry September moves in
# hundredths of a cfs, so a purely relative test calls noise a trend; a
# purely absolute one is deaf to a river running at hundreds of cfs. Require
# both: a change of at least 10% and of at least 0.05 cfs.
TREND_WINDOW_HOURS = 24 * 7
TREND_MIN_RELATIVE_CHANGE = 0.10
TREND_MIN_ABSOLUTE_CHANGE_CFS = 0.05


def _fetch_watershed_trend(observed_at: str) -> str:
    """Direction of discharge over the seven days ending at observedAt.

    Compares mean discharge in the older half of the window against the
    newer half, and only over stations present in both halves — a gauge that
    drops out mid-window would otherwise move the aggregate by itself and
    read as a trend in the river.

    Returns one of the lexicon's knownValues. "unknown" means there wasn't
    enough data to say, which is what this field claimed unconditionally
    before it was computed at all.
    """
    db_path = DB_PATHS["watershed"]
    if not db_path.exists():
        return "unknown"

    end = _anchor(observed_at)
    start = end - timedelta(hours=TREND_WINDOW_HOURS)
    midpoint = end - timedelta(hours=TREND_WINDOW_HOURS / 2)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT station_id, collected_at, value
            FROM readings
            WHERE parameter_code = '00060' AND value IS NOT NULL
              AND collected_at >= ? AND collected_at <= ?
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return "unknown"

    mid_iso = midpoint.isoformat()
    halves: dict[str, list] = {}
    for r in rows:
        older, newer = halves.setdefault(r["station_id"], ([], []))
        (older if r["collected_at"] < mid_iso else newer).append(r["value"])

    older_total = newer_total = 0.0
    stations_compared = 0
    for older, newer in halves.values():
        if not older or not newer:
            continue
        older_total += sum(older) / len(older)
        newer_total += sum(newer) / len(newer)
        stations_compared += 1

    if not stations_compared:
        return "unknown"

    delta = newer_total - older_total
    threshold = max(TREND_MIN_ABSOLUTE_CHANGE_CFS,
                    TREND_MIN_RELATIVE_CHANGE * older_total)
    if abs(delta) < threshold:
        return "stable"
    return "rising" if delta > 0 else "falling"


def _fetch_aqi_numerics(observed_at: str) -> dict:
    db_path = DB_PATHS["aqi"]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff = _stale_cutoff(observed_at, READING_MAX_AGE_HOURS)
        rows = conn.execute(
            """
            SELECT parameter, aqi
            FROM observations
            WHERE parameter IN ('PM2.5', 'OZONE') AND aqi IS NOT NULL
              AND collected_at >= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            LIMIT 10
            """,
            (cutoff, observed_at),
        ).fetchall()
        conn.close()
        result = {}
        seen: set = set()
        for r in rows:
            param = r["parameter"]
            if param not in seen:
                seen.add(param)
                if param == "PM2.5":
                    result["pm25Aqi"] = r["aqi"]
                elif param == "OZONE":
                    result["ozoneAqi"] = r["aqi"]
        return result
    except sqlite3.Error:
        return {}


# Staleness bounds for the numeric fields attached to a published record.
#
# Every collector DB keeps its rows forever, and these queries pick the row
# nearest in time to the observation with no lower bound on how far "nearest"
# may be. That is fine while the collectors are running and catastrophic when
# one stops: the publisher goes on attaching the last reading it ever saw to
# every subsequent record, with nothing marking it stale, and Synthesis reads
# those numbers as current conditions.
#
# Fire is the case that bit us — its query ordered by distance rather than
# time, so a single old detection stayed "nearest" indefinitely and a
# five-day-old hotspot was published as an 8.5-mile threat while the agent's
# own summary said nothing was inside 20 miles.
#
# The bound is measured from observedAt, not from now. A record describes the
# moment it claims to, and republishing a backlog (as happened on 2026-08-26)
# should attach the readings from that moment rather than today's.
_FIRE_DAY_RANGE = _NODE_CFG["fire"].get("day_range", 2)
NEAREST_HOTSPOT_MAX_AGE_HOURS = _FIRE_DAY_RANGE * 24 + 24  # matches Fire/mcp_server.py

# Collectors poll every 15-30 minutes, so a reading hours old means the
# collector is struggling. Wide enough to ride out a few missed polls, narrow
# enough that a dead collector produces an absent field instead of a fiction.
READING_MAX_AGE_HOURS = 6


def _anchor(observed_at: str) -> datetime:
    """The moment a record claims to describe, as a datetime.

    Falls back to now if observedAt can't be parsed — an unreadable
    timestamp should narrow a query window, never widen it.
    """
    try:
        anchor = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return anchor
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _stale_cutoff(observed_at: str, hours: float) -> str:
    """ISO cutoff `hours` before *observed_at*, for use as a SQL lower bound."""
    return (_anchor(observed_at) - timedelta(hours=hours)).isoformat()


def _fetch_fire_numerics(observed_at: str) -> dict:
    """Return the nearest currently-relevant hotspot's distance/confidence/FRP.

    "Nearest" means nearest in distance among hotspots still inside the
    currency window — the same set the agent saw via get_nearest_hotspots, so
    the numeric fields describe the hotspot the summary is actually about. The
    lexicon defines nearestHotspotDistanceMi as "the nearest hotspot used in
    this observation", and that is only true if both sides apply the same
    window.

    Returns no distance/confidence/frp at all when nothing is current, so
    build_fire_record omits those fields rather than publishing a stale
    reading. hotspotCount keeps its own 6-hour window, which the lexicon
    documents separately.
    """
    db_path = DB_PATHS["fire"]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff = _stale_cutoff(observed_at, NEAREST_HOTSPOT_MAX_AGE_HOURS)
        row = conn.execute(
            """
            SELECT distance_mi, confidence, frp
            FROM hotspots
            WHERE collected_at >= ? AND distance_mi IS NOT NULL
            ORDER BY distance_mi ASC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        count_row = conn.execute(
            """
            SELECT COUNT(*) as n
            FROM hotspots
            WHERE ABS(strftime('%s', collected_at) - strftime('%s', ?)) < 3600 * 6
            """,
            (observed_at,),
        ).fetchone()
        conn.close()
        result = dict(row) if row else {}
        if count_row:
            result["hotspotCount"] = count_row["n"]
        return result
    except sqlite3.Error:
        return {}


def _atproto_safe(value):
    """ATProto records are DAG-CBOR — no IEEE float type is valid in the
    data model (only null/boolean/integer/string/cid/bytes/array/object).
    Stringify floats to preserve exact values instead of lossily rounding."""
    return str(value) if isinstance(value, float) else value


# ---------------------------------------------------------------------------
# Observation builders — convert DB rows to lexicon records
# ---------------------------------------------------------------------------

def build_watershed_record(row: dict, observed_at: str) -> dict:
    numerics = _fetch_watershed_numerics(observed_at)
    # Fall back to the configured stations only when nothing is current —
    # an empty list would read as "no gauges exist" rather than "no gauge
    # reported in time".
    station_ids = (numerics.pop("stationIds", None)
                   or list(_NODE_CFG["watershed"]["usgs_stations"]))
    watershed_block: dict = {
        "stationIds": [f"USGS-{sid}" for sid in station_ids],
        "sevenDayTrend": _fetch_watershed_trend(observed_at),
    }
    for field in ("dischargeMinCfs", "dischargeMeanCfs", "dischargeMaxCfs",
                  "gageHeightMinFt", "gageHeightMaxFt"):
        if field in numerics:
            watershed_block[field] = _atproto_safe(numerics[field])

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#watershed",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": row.get("model") or "unknown",
        "watershed": watershed_block,
    }


def build_weather_record(row: dict, observed_at: str) -> dict:
    numerics = _fetch_weather_numerics(observed_at)
    weather_block: dict = {
        "stationId": _NODE_CFG["weather"]["observation_station"],
        "activeAlerts": [],
    }
    for field in ("temperature_f", "humidity_pct", "wind_speed_mph",
                  "wind_direction_deg", "wind_gust_mph", "precip_24h_mm"):
        val = numerics.get(field)
        if val is not None:
            weather_block[field] = _atproto_safe(val)

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#weather",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": row.get("model") or "unknown",
        "weather": weather_block,
    }


def build_aqi_record(row: dict, observed_at: str) -> dict:
    numerics = _fetch_aqi_numerics(observed_at)
    aqi_block: dict = {
        "reportingArea": _NODE_CFG["aqi"]["reporting_area"],
    }
    if "pm25Aqi" in numerics:
        aqi_block["pm25Aqi"] = _atproto_safe(numerics["pm25Aqi"])
    if "ozoneAqi" in numerics:
        aqi_block["ozoneAqi"] = _atproto_safe(numerics["ozoneAqi"])

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#aqi",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": row.get("model") or "unknown",
        "aqi": aqi_block,
    }


def build_fire_record(row: dict, observed_at: str) -> dict:
    numerics = _fetch_fire_numerics(observed_at)
    # node_config.json's fire.source (single string) became fire.sources
    # (a list) when multi-satellite VIIRS polling was added — this crashed
    # every fire publish attempt with a KeyError until caught, since this
    # line was never updated to match. Joined into a single string here to
    # avoid also changing the lexicon field's type from string to array.
    sources = _NODE_CFG["fire"].get("sources") or [_NODE_CFG["fire"].get("source", "unknown")]
    fire_block: dict = {
        "bbox": _NODE_CFG["fire"]["bbox"],
        "source": ",".join(sources),
    }
    if "distance_mi" in numerics and numerics["distance_mi"] is not None:
        fire_block["nearestHotspotDistanceMi"] = _atproto_safe(numerics["distance_mi"])
    if "confidence" in numerics and numerics["confidence"] is not None:
        fire_block["nearestHotspotConfidence"] = numerics["confidence"]
    if "frp" in numerics and numerics["frp"] is not None:
        fire_block["nearestHotspotFrpMw"] = _atproto_safe(numerics["frp"])
    if "hotspotCount" in numerics:
        fire_block["hotspotCount"] = numerics["hotspotCount"]

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#fire",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": row.get("model") or "unknown",
        "fire": fire_block,
    }


# ---------------------------------------------------------------------------
# Domain publication
# ---------------------------------------------------------------------------

DOMAIN_CONFIG = {
    "watershed": {
        "table": "agent_observations",
        "db_key": "watershed",
        "builder": build_watershed_record,
    },
    "weather": {
        "table": "agent_observations",
        "db_key": "weather",
        "builder": build_weather_record,
    },
    "aqi": {
        "table": "agent_observations",
        "db_key": "aqi",
        "builder": build_aqi_record,
    },
    "fire": {
        "table": "agent_observations",
        "db_key": "fire",
        "builder": build_fire_record,
    },
}


def get_unpublished(domain: str, pub_conn: sqlite3.Connection) -> list[dict]:
    """Get observations from domain DB that haven't been published yet."""
    db_path = DB_PATHS[domain]
    if not db_path.exists():
        log.warning("DB not found for domain '%s': %s", domain, db_path)
        return []

    config = DOMAIN_CONFIG[domain]
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # No LIMIT here on purpose. This used to cap at the 20 most recent
        # rows by rowid — invisible under normal operation (each run only
        # ever has 1-2 new rows to publish), but it's not a sliding cursor:
        # once a real backlog exceeds 20 (a publish-side bug going unnoticed
        # for a while, say), anything older than the newest 20 falls
        # permanently outside the window and never gets picked up by any
        # future run, even after the underlying bug is fixed. Confirmed in
        # practice — 37 unpublished Fire rows piled up behind a publisher
        # crash; only the newest 20 of them would ever have been recovered
        # under the old query. A sane upper bound (10000) guards against a
        # truly pathological runaway backlog without reintroducing the same
        # silent-data-loss shape at a higher threshold.
        rows = conn.execute(
            f"SELECT rowid AS source_id, * FROM {config['table']} ORDER BY rowid DESC LIMIT 10000"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        log.error("Failed to read %s DB: %s", domain, exc)
        return []

    unpublished = []
    for row in rows:
        source_id = row["source_id"]
        if not already_published(pub_conn, domain, source_id):
            unpublished.append(dict(row) | {"_source_id": source_id})

    return unpublished


def publish_domain(domain: str, session: BlueskySession,
                   pub_conn: sqlite3.Connection, dry_run: bool) -> int:
    unpublished = get_unpublished(domain, pub_conn)
    if not unpublished:
        log.info("[%s] Nothing new to publish", domain)
        return 0

    config = DOMAIN_CONFIG[domain]
    count = 0

    for row in reversed(unpublished):  # oldest first
        source_id = row["_source_id"]
        observed_at = row.get("observed_at") or row.get("observedAt", "")

        # Normalise timestamp to Z suffix
        if observed_at and "+00:00" in observed_at:
            observed_at = observed_at.replace("+00:00", "Z")
        if observed_at and not observed_at.endswith("Z"):
            observed_at += "Z"

        record = config["builder"](row, observed_at)
        flagged = bool(row.get("flagged", False))

        if dry_run:
            log.info("[%s] [DRY RUN] Would publish observation %d:", domain, source_id)
            log.info("  Record: %s", json.dumps(record)[:200] + "...")
            log.info("  Flagged: %s", flagged)
            count += 1
            continue

        try:
            # Publish the structured lexicon record. This PDS is the node's
            # own — domain agents write for other agents to consume, not for
            # a human Bluesky audience, so there's no accompanying app.bsky
            # post here. That's Synthesis's job, on its own identity.
            record_uri = session.create_record(LEXICON, record)
            log.info("[%s] Published lexicon record: %s", domain, record_uri)

            mark_published(pub_conn, domain, source_id, observed_at, record_uri, flagged)
            count += 1

        except httpx.HTTPStatusError as exc:
            log.error("[%s] Failed to publish observation %d: %s — %s",
                      domain, source_id, exc, exc.response.text)
        except httpx.HTTPError as exc:
            log.error("[%s] Failed to publish observation %d: %s", domain, source_id, exc)

    return count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ATProto publisher")
    parser.add_argument(
        "--domain",
        choices=["all", "watershed", "weather", "aqi", "fire"],
        default="all",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    handle = os.environ.get("BSKY_HANDLE", "").strip()
    app_password = os.environ.get("BSKY_APP_PASSWORD", "").strip()

    if not handle or not app_password:
        raise RuntimeError(
            "BSKY_HANDLE and BSKY_APP_PASSWORD must be set in /etc/environment. "
            "Create a Bluesky account for this node and generate an App Password."
        )

    pub_conn = init_publisher_db()

    domains = (
        ["watershed", "weather", "aqi", "fire"]
        if args.domain == "all"
        else [args.domain]
    )

    log.info("=== ATProto Publisher starting ===")
    log.info("Domains: %s  |  Dry run: %s", domains, args.dry_run)

    if not args.dry_run:
        session = BlueskySession(handle, app_password)
        session.login()
    else:
        session = None

    # Each domain is published independently. An unexpected error in one
    # domain's builder used to abort the whole run, so every domain after
    # it in the list silently published nothing — the failure looked like a
    # single-domain problem in the log while actually costing all of them.
    # Fire is last here, which is the only reason the fire.source KeyError
    # (fixed in #44) cost just fire and not weather and aqi too. Ordering
    # shouldn't be what protects the other domains.
    total = 0
    failed: list[str] = []
    for domain in domains:
        try:
            count = publish_domain(domain, session, pub_conn, dry_run=args.dry_run)
        except Exception:
            # log.exception keeps the traceback in publisher.log — that
            # traceback is how #44 was diagnosed, so isolation must not
            # come at the cost of losing it.
            log.exception("[%s] Unhandled error — skipping domain", domain)
            failed.append(domain)
            continue
        log.info("[%s] Published %d record(s)", domain, count)
        total += count

    if failed:
        # Still exit non-zero. Isolating the failure must not turn a broken
        # run into a silent success for cron — this bug ran twice a day for
        # a month and was caught by a gap in the Viewer, not by an alarm.
        log.error("=== Publisher finished with errors — %d record(s) published, "
                  "failed domains: %s ===", total, ", ".join(failed))
        sys.exit(1)

    log.info("=== Publisher complete — %d total records published ===", total)


if __name__ == "__main__":
    main()
