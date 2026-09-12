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
import importlib.util
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

# The note marker, so a note recorded next to a fired rule never reaches a
# published record as though it were the reason for the flag.
sys.path.insert(0, str(BASE))
from agent_runtime import is_note  # noqa: E402


def _lexicon_max_bytes(field: str, default: int) -> int:
    """A field's maxLength, read from the lexicon rather than restated here.

    The lexicon is the contract this publisher writes against, so a cap
    copied into Python is a second copy of a number that can drift from the
    thing enforcing it — the shape that produced the four-copies threshold
    drift and the currency window that existed in three places and was
    missing from a fourth.

    Falls back to *default* rather than failing: an unreadable lexicon file
    must not stop a node publishing. Over-long output would be rejected by
    the PDS loudly, so the failure mode of a stale fallback is visible.
    """
    try:
        doc = json.loads(
            (Path(__file__).parent / "lexicon" / f"{LEXICON}.json").read_text())
        props = doc["defs"]["main"]["record"]["properties"]
        return int(props[field]["maxLength"])
    except (OSError, KeyError, ValueError, TypeError) as exc:
        log.warning("Could not read %s maxLength from the lexicon (%s); "
                    "using %d", field, exc, default)
        return default


def _load_domain_thresholds(domain: str):
    """Load a domain's thresholds module by path.

    The publisher lives outside the domain packages and is built into its own
    image, so a plain import won't reach them; loading by explicit file path
    keeps one definition of a window shared without putting a module named
    `thresholds` on sys.path, where four domains would collide.

    A missing module is fatal rather than defaulted. The whole point is that
    the publisher and the domain agent scope the same rows, and a silent
    fallback to a locally-guessed window is exactly the divergence this
    removes — a five-day-old hotspot published as an 8.5-mile threat.
    """
    path = BASE / domain / "thresholds.py"
    spec = importlib.util.spec_from_file_location(f"_{domain}_thresholds", path)
    if spec is None or spec.loader is None:      # pragma: no cover - unreachable
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FIRE_THRESHOLDS = _load_domain_thresholds("Fire")
_WEATHER_THRESHOLDS = _load_domain_thresholds("Weather")
MEANINGFUL_RAIN_1H_MM = _WEATHER_THRESHOLDS.MEANINGFUL_RAIN_1H_MM

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

# Resolved after logging is configured, because the fallback path warns.
FLAG_REASON_MAX_BYTES = _lexicon_max_bytes("flagReason", 200)


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

# SQLite column to lexicon field. The publisher spent its whole life emitting
# the raw column names, which weatherData has never declared — a consumer
# reading the lexicon and looking for windGustMph found nothing, while the
# actual records carried wind_gust_mph. Rename at the fetch boundary so the
# builder only ever handles declared names.
_WEATHER_FIELD_MAP = {
    "temperature_f":      "temperatureF",
    "humidity_pct":       "humidityPct",
    "wind_speed_mph":     "windSpeedMph",
    "wind_direction_deg": "windDirectionDeg",
    "wind_gust_mph":      "windGustMph",
    "precip_24h_mm":      "precipMm24h",
}


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
            WHERE collected_at >= ? AND collected_at <= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            LIMIT 1
            """,
            (cutoff, observed_at, observed_at),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return {field: row[column] for column, field in _WEATHER_FIELD_MAP.items()
            if row[column] is not None}


def _fetch_dry_spell(observed_at: str) -> dict:
    """Days since measurable rain as of observedAt, bounded by the record.

    Mirrors Weather/mcp_server.py's _dry_spell so the published record carries
    the same fact the agent reasoned from. Publishing it as a field is the
    point: synthesis had been carrying a "147+ consecutive precipitation-free
    days" counter in prose, incrementing itself run to run with nothing
    measuring it, while the deepest substantiated claim available was "none in
    the last 7 days".

    Returns at most one of daysSinceMeasurableRain (rain found) or
    dryRecordDays (none found anywhere), never both — a consumer must not be
    able to read the record's length as a drought's length.
    """
    db_path = DB_PATHS["weather"]
    if not db_path.exists():
        return {}
    anchor = _anchor(observed_at)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        last_rain = conn.execute(
            """
            SELECT collected_at FROM observations
            WHERE precip_1h_mm > ? AND collected_at <= ?
            ORDER BY collected_at DESC LIMIT 1
            """,
            (MEANINGFUL_RAIN_1H_MM, anchor.isoformat()),
        ).fetchone()
        first = conn.execute(
            "SELECT MIN(collected_at) AS t FROM observations WHERE collected_at <= ?",
            (anchor.isoformat(),),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return {}

    if last_rain:
        since = _parse_iso(last_rain["collected_at"])
        if since is None:
            return {}
        return {"daysSinceMeasurableRain": max(0, (anchor - since).days)}
    if first and first["t"]:
        start = _parse_iso(first["t"])
        if start is None:
            return {}
        return {"dryRecordDays": max(0, (anchor - start).days)}
    return {}


def _fetch_active_alerts(observed_at: str) -> list[str]:
    """NWS alert event names in effect at observedAt.

    This field was published as an empty list on every weather record ever
    written, which is not "no alerts known" but a positive assertion that
    none were active — and the collector has been storing them in the alerts
    table the whole time. Synthesis reads activeAlerts to confirm fire
    predictions against FIRE_CONFIRM_ALERTS; that branch could never fire.

    Alerts are filtered by their own onset/expires rather than by collection
    time, so a Red Flag Warning polled six hours ago and still in effect is
    reported and one that has since expired is not. An alert whose window
    won't parse is included: the collector saw it inside the staleness
    window, and dropping it silently is the failure this fixes.
    """
    db_path = DB_PATHS["weather"]
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT event, onset, expires
            FROM alerts
            WHERE collected_at >= ? AND event IS NOT NULL AND event != ''
            ORDER BY collected_at DESC
            """,
            (_stale_cutoff(observed_at, READING_MAX_AGE_HOURS),),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    anchor = _anchor(observed_at)
    events: list[str] = []
    for r in rows:
        expires = _parse_iso(r["expires"])
        if expires is not None and expires < anchor:
            continue
        onset = _parse_iso(r["onset"])
        if onset is not None and onset > anchor:
            continue
        event = r["event"].strip()
        if event and event not in events:
            events.append(event)
    return events


# Below this speed the vane is reporting noise, not a pattern.
CALM_WIND_MPH = 3.0

# Offshore quadrant, NNE through ESE. Same range as Synthesis's _is_diablo,
# which carries the meteorological rationale; the two are separate
# deployments and each needs its own copy.
DIABLO_ARC_DEG = (22.0, 112.0)

# South through WNW: San Pablo Bay is south of Napa and the Petaluma Gap
# southwest, so this is where marine air arrives from.
MARINE_ARC_DEG = (180.0, 300.0)


def _wind_pattern(direction_deg, speed_mph) -> str:
    """Classify wind pattern from direction and speed.

    Deliberately never returns "valley", though the lexicon lists it. Napa
    Valley runs NNW-SSE, so up-valley flow arrives from the same southerly
    sector as marine air and direction alone cannot separate them. Returning
    "unknown" for a sector we can't classify is worth more to a consumer than
    a category that might be wrong — the same reason fireRisk stays absent
    rather than being inferred from the numbers.
    """
    speed = _parse_float(speed_mph)
    if speed is not None and speed < CALM_WIND_MPH:
        return "calm"
    deg = _parse_float(direction_deg)
    if deg is None:
        return "unknown"
    deg %= 360
    if DIABLO_ARC_DEG[0] <= deg <= DIABLO_ARC_DEG[1]:
        return "diablo"
    if MARINE_ARC_DEG[0] <= deg <= MARINE_ARC_DEG[1]:
        return "marine"
    return "unknown"


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
              AND collected_at >= ? AND collected_at <= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            """,
            (cutoff, observed_at, observed_at),
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
              AND collected_at >= ? AND collected_at <= ?
            ORDER BY ABS(strftime('%s', collected_at) - strftime('%s', ?))
            LIMIT 10
            """,
            (cutoff, observed_at, observed_at),
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
NEAREST_HOTSPOT_MAX_AGE_HOURS = _FIRE_THRESHOLDS.NEAREST_HOTSPOT_MAX_AGE_HOURS

# Collectors poll every 15-30 minutes, so a reading hours old means the
# collector is struggling. Wide enough to ride out a few missed polls, narrow
# enough that a dead collector produces an absent field instead of a fiction.
READING_MAX_AGE_HOURS = 6


def _parse_iso(value) -> datetime | None:
    """An ISO8601 timestamp as an aware datetime, or None if it won't parse.

    Naive timestamps are read as UTC — every collector writes UTC — while
    NWS alert windows arrive with a local offset, so comparisons have to go
    through datetimes rather than string ordering.
    """
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_float(value) -> float | None:
    """Numeric value as a float, or None. Accepts the strings the publisher
    itself emits, so a value can be re-read after _atproto_safe."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _anchor(observed_at: str) -> datetime:
    """The moment a record claims to describe, as a datetime.

    Falls back to now if observedAt can't be parsed — an unreadable
    timestamp should narrow a query window, never widen it.
    """
    return _parse_iso(observed_at) or datetime.now(timezone.utc)


def _stale_cutoff(observed_at: str, hours: float) -> str:
    """ISO cutoff `hours` before *observed_at*, for use as a SQL lower bound."""
    return (_anchor(observed_at) - timedelta(hours=hours)).isoformat()



def _hotspot_detected_at(numerics: dict) -> datetime | None:
    """When the satellite saw the nearest hotspot: acq_date plus acq_time."""
    dt = _parse_iso(numerics.get("acq_date"))
    if dt is None:
        return None
    raw = str(numerics.get("acq_time") or "").strip().zfill(4)
    if raw.isdigit() and len(raw) == 4:
        dt = dt.replace(hour=int(raw[:2]) % 24, minute=int(raw[2:]) % 60)
    return dt


def _was_burning(inc, when) -> bool:
    """Could this incident have produced a detection at *when*?

    Mirrors Fire/mcp_server._was_burning, and exists separately because the
    publisher ships in its own image. Distance alone is not a match: on
    2026-09-09 the five incidents nearest Napa were all 100% contained, so a
    location-only test would publish a fresh detection as a known, closed
    event — which is the direction that hides a real fire.
    """
    if when is None:
        return True
    start = _parse_iso(inc["started_at"])
    if start is None:
        return True
    if when < start - timedelta(days=_FIRE_THRESHOLDS.INCIDENT_MATCH_LEAD_DAYS):
        return False
    if inc["is_active"]:
        return True
    end = _parse_iso(inc["extinguished_at"]) or _parse_iso(inc["updated_at"])
    if end is None:
        return True
    return when <= end + timedelta(days=_FIRE_THRESHOLDS.INCIDENT_MATCH_TAIL_DAYS)


def _is_burning_at(inc, when) -> bool:
    """Is this incident burning at *when*?

    Distinct from _was_burning, which asks whether an incident could have
    produced a detection at a given moment and therefore allows a tail after
    containment — a satellite sees heat in a scar. This asks the narrower
    question the standalone nearest-incident field needs: is there fire there
    now. No tail, because a fire that closed yesterday is not a nearby fire.

    An incident whose end time is unknown counts as burning. That errs toward
    reporting a fire rather than hiding one, the same direction _was_burning
    chose.
    """
    if when is None:
        return bool(inc["is_active"])
    start = _parse_iso(inc["started_at"])
    if start is not None and when < start:
        return False
    if inc["is_active"]:
        return True
    end = _parse_iso(inc["extinguished_at"]) or _parse_iso(inc["updated_at"])
    if end is None:
        return True
    return when <= end


def _fetch_incident_context(observed_at: str, hotspot_lat=None, hotspot_lon=None,
                            detected_at: datetime | None = None) -> dict:
    """Named CAL FIRE incident matching the nearest hotspot, plus the nearest
    incident actively burning at observedAt.

    The statewide figure is deliberately not bounded by the FIRMS box: on
    2026-09-09 the nearest real wildfire was at Willits, 96 miles out and
    outside the box, so the satellite feed could not see it. This is the only
    field that carries such a fire into the record.

    It is filtered to incidents actually burning, which it was not until
    2026-09-10, and the omission made the field worse than useless. CAL FIRE's
    feed holds every incident of the year — 483 closed against typically zero
    or one active — so an unfiltered nearest-by-distance sort names a dead
    fire on essentially every run. The 2026-09-10 synthesis record read
    `nearestIncidentName: Mason Fire` at 7.45 miles as corroborating evidence
    of current fire activity and wrote "multiple active fire signatures in the
    region"; the Mason Fire burned for eight hours on 2026-06-18 and had been
    out for three months.

    Worse, the field could never have served the purpose it was added for. A
    nearest-by-distance sort across all history puts a long-extinguished local
    fire ahead of a distant burning one, so Willits at 96 miles would have
    lost to Mason at 7.45 every time. Filtering is what makes the Willits case
    work at all, not a restriction on it.

    A match is a positive identifier only. No match emits no field at all
    rather than an empty string, because absence here means "no published
    incident corresponds", not "not a fire": publication lags ignition, and
    the feed is a curated subset rather than a census — 484 incidents
    statewide for all of 2026, with no prescribed-burn category.
    """
    db_path = DB_PATHS["fire"]
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT name, latitude, longitude, acres_burned,
                      percent_contained, distance_mi, started_at, updated_at,
                      extinguished_at, is_active
               FROM incidents
               WHERE latitude IS NOT NULL AND longitude IS NOT NULL
               ORDER BY distance_mi ASC"""
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}          # table absent until incidents_collector.py has run
    if not rows:
        return {}

    result: dict = {}
    # rows is already ordered by distance, so the first burning one is the
    # nearest burning one.
    as_of = _parse_iso(observed_at)
    nearest = next((r for r in rows if _is_burning_at(r, as_of)), None)
    if nearest is not None and nearest["name"] and nearest["distance_mi"] is not None:
        result["nearestIncidentName"] = nearest["name"]
        result["nearestIncidentDistanceMi"] = _atproto_safe(nearest["distance_mi"])

    if hotspot_lat is None or hotspot_lon is None:
        return result
    best = None
    for inc in rows:
        if not _was_burning(inc, detected_at):
            continue
        try:
            d = _haversine_mi(float(hotspot_lat), float(hotspot_lon),
                              float(inc["latitude"]), float(inc["longitude"]))
        except (TypeError, ValueError):
            continue
        if d <= _FIRE_THRESHOLDS.incident_match_radius_mi(inc["acres_burned"]) and (
                best is None or d < best[0]):
            best = (d, inc)
    if best is not None and best[1]["name"]:
        result["nearestHotspotIncidentName"] = best[1]["name"]
        if best[1]["percent_contained"] is not None:
            result["nearestHotspotIncidentContainmentPct"] = int(
                round(float(best[1]["percent_contained"])))
    return result


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles. Same formula as Fire/collector.py; the
    publisher ships in its own image and cannot import it."""
    import math
    r_mi = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return r_mi * 2 * math.asin(math.sqrt(a))


def _frp_percentile(conn: sqlite3.Connection, frp) -> int | None:
    """Where an FRP reading sits in this collector's own history, 0-100.

    Its own history, never the region's: this node has been collecting for
    months, not decades, so the percentile says "unusual for us" and cannot
    say "unusual for Napa". The lexicon carries that caveat too.
    """
    if frp is None:
        return None
    dist = conn.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(CASE WHEN frp <= ? THEN 1 ELSE 0 END) AS at_or_below"
        " FROM hotspots WHERE frp IS NOT NULL",
        (frp,),
    ).fetchone()
    if dist and dist["n"]:
        return round(100.0 * (dist["at_or_below"] or 0) / dist["n"])
    return None


def _fetch_fire_numerics(observed_at: str) -> dict:
    """Return the nearest currently-relevant hotspot, and the most energetic.

    "Nearest" means nearest in distance among hotspots still inside the
    currency window — the same set the agent saw via get_nearest_hotspots, so
    the numeric fields describe the hotspot the summary is actually about. The
    lexicon defines nearestHotspotDistanceMi as "the nearest hotspot used in
    this observation", and that is only true if both sides apply the same
    window.

    The peak is published alongside it because nearest is not a proxy for
    most significant, and within a cluster it is close to arbitrary which
    detection happens to be nearest. On 2026-09-09 22:00 the Steele Fire
    record carried nearestHotspotFrpMw 7.47 at the 79th percentile with
    confidence "n", while its own summary described the same 14.0-14.4 mile
    cluster as holding four high-confidence detections at 24.1-33.5 MW and
    the 96th-98th percentiles. Both were true of different hotspots 0.4 miles
    apart, and a consumer reading only the structured fields got the calm
    version of an active wildfire.

    This publishes a measurement, not a judgement. What the peak means is the
    reader's to decide — the record's job is to not make them infer a number
    the collector already has.

    The peak is drawn from exactly the population the nearest comes from —
    same currency window, same NOT NULL filter — because two numbers taken
    from different sets cannot be compared, which was the original defect in
    another form.

    Returns no distance/confidence/frp at all when nothing is current, so
    build_fire_record omits those fields rather than publishing a stale
    reading. hotspotCount now comes from this same window too — it had its
    own ±6h one, which made it the third population described by one fire
    block and let it read 0 while ten hotspots sat current in the table.
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
            SELECT distance_mi, confidence, frp, latitude, longitude,
                   acq_date, acq_time
            FROM hotspots
            WHERE collected_at >= ? AND distance_mi IS NOT NULL
            ORDER BY distance_mi ASC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        # Same population as nearest and peak: the currency window, with a
        # distance. It used to be its own ±6h window with no distance filter,
        # counting how many detections were first STORED near observedAt
        # rather than how many hotspots were current — see thresholds.py.
        count_row = conn.execute(
            """
            SELECT COUNT(*) as n
            FROM hotspots
            WHERE collected_at >= ? AND distance_mi IS NOT NULL
            """,
            (cutoff,),
        ).fetchone()
        # The most energetic hotspot in the same window. Ordered by FRP rather
        # than distance; ties break on the nearer one so the field is stable
        # across runs instead of picking arbitrarily among equals.
        peak = conn.execute(
            """
            SELECT distance_mi, confidence, frp
            FROM hotspots
            WHERE collected_at >= ? AND distance_mi IS NOT NULL
              AND frp IS NOT NULL
            ORDER BY frp DESC, distance_mi ASC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()

        # Where this reading sits in the collector's own FRP history, so a
        # consumer has a baseline instead of a bare MW figure. Synthesis
        # called a 66 MW detection "well beyond anything previously reported"
        # when two comparable ones were already in this table.
        frp_pct = _frp_percentile(conn, row["frp"]) if row else None
        peak_pct = _frp_percentile(conn, peak["frp"]) if peak else None
        conn.close()
        result = dict(row) if row else {}
        if frp_pct is not None:
            result["frpPercentile"] = frp_pct
        if peak is not None:
            # Emitted even when the peak IS the nearest hotspot. Omitting it
            # then would make absence mean "same as nearest", which is a fact
            # the reader would have to infer — the thing this record keeps
            # getting wrong.
            result["maxFrp"] = peak["frp"]
            result["maxFrpDistanceMi"] = peak["distance_mi"]
            result["maxFrpConfidence"] = peak["confidence"]
            if peak_pct is not None:
                result["maxFrpPercentile"] = peak_pct
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
# flagReason
# ---------------------------------------------------------------------------
#
# Domain records published `flagged: true` with `flagReason: ""` for the
# entire life of this system. On 2026-09-09 that included an observation whose
# summary opened "ACTIVE WILDFIRE" — anyone filtering the firehose on
# flagReason got an empty string for the most consequential fire record the
# node has produced.
#
# It stayed empty because there was nothing honest to put in it. The agents
# return summary, flagged and reasoning; none of those is a one-line reason,
# and reasoning is long-form model prose that would have been truncated into
# a fragment and published as though it were a label. Filling the field with
# the first 200 bytes of that would have been the agentModel defect again: a
# structured field asserting something nobody checked.
#
# flag_rules.py made a real answer available. The rules evaluate the same
# criteria the prompt states, independently of the model, and record which one
# matched and on what values. That is a reason, computed rather than narrated.


def _fit(text: str, max_bytes: int) -> str:
    """Truncate to a byte limit on a character boundary."""
    encoded = (text or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return text or ""
    return encoded[:max_bytes - 3].decode("utf-8", errors="ignore") + "…"


def _fired_rules(row: dict) -> list[str]:
    """Rules that matched on this run, excluding notes.

    A note is measured and recorded but never contributed to the flag — see
    agent_runtime.NOTE_PREFIX — so it must not be published as the reason for
    one. Fire's newness signal is a note, and it is present on most runs.
    """
    try:
        entries = json.loads(row.get("rules_fired") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(e) for e in entries if not is_note(e)]


def _flag_reason(row: dict) -> str:
    """One line saying why this record is flagged, or "" when it is not.

    The published `flagged` bit is the model's, and the rules are shadow —
    they have never overridden it. So this explains the flag with the best
    evidence available and says plainly when there is none, rather than
    implying the rule is why the model decided:

      a rule matched      the rule and the values that matched it
      no rule matched     the flag is judgement with no arithmetic behind it
      no verdict          the rules did not run, which is not the same thing

    The third case matters. A row from before shadow recording, or one where
    rule evaluation raised, has rules_fired NULL — reporting that as "no rule
    matched" would assert a negative result that was never computed.
    """
    if not bool(row.get("flagged", False)):
        return ""
    fired = _fired_rules(row)
    if fired:
        return _fit("; ".join(fired), FLAG_REASON_MAX_BYTES)
    if row.get("rules_flagged") is None:
        return "model judgement; no rule verdict was recorded for this run"
    return "model judgement; no deterministic rule matched"


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
        "flagReason": _flag_reason(row),
        "agentModel": row.get("model") or "unknown",
        "watershed": watershed_block,
    }


def build_weather_record(row: dict, observed_at: str) -> dict:
    numerics = _fetch_weather_numerics(observed_at)
    weather_block: dict = {
        "stationId": _NODE_CFG["weather"]["observation_station"],
        "activeAlerts": _fetch_active_alerts(observed_at),
    }
    for field in _WEATHER_FIELD_MAP.values():
        val = numerics.get(field)
        if val is not None:
            weather_block[field] = _atproto_safe(val)

    # Derived here rather than asked of the model: it's a lookup from a number
    # the record already carries, and Synthesis's Diablo branch has had
    # nothing to read since it was written.
    pattern = _wind_pattern(numerics.get("windDirectionDeg"),
                            numerics.get("windSpeedMph"))
    if pattern != "unknown":
        weather_block["windPattern"] = pattern

    weather_block.update(_fetch_dry_spell(observed_at))

    # fireRisk stays absent. The lexicon calls it "the weather agent's
    # assessment", and Weather/agent.py returns only summary, flagged and
    # reasoning — there is no assessment to publish. Deriving one from the
    # thresholds here would put a number in the agent's mouth.

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#weather",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": _flag_reason(row),
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
        "flagReason": _flag_reason(row),
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
    if "frpPercentile" in numerics:
        fire_block["nearestHotspotFrpPercentile"] = numerics["frpPercentile"]
    if numerics.get("maxFrp") is not None:
        fire_block["maxHotspotFrpMw"] = _atproto_safe(numerics["maxFrp"])
        fire_block["maxHotspotDistanceMi"] = _atproto_safe(numerics["maxFrpDistanceMi"])
        if numerics.get("maxFrpConfidence"):
            fire_block["maxHotspotConfidence"] = numerics["maxFrpConfidence"]
        if "maxFrpPercentile" in numerics:
            fire_block["maxHotspotFrpPercentile"] = numerics["maxFrpPercentile"]
    if "hotspotCount" in numerics:
        fire_block["hotspotCount"] = numerics["hotspotCount"]
    fire_block.update(_fetch_incident_context(
        observed_at, numerics.get("latitude"), numerics.get("longitude"),
        _hotspot_detected_at(numerics)))

    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#fire",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": _flag_reason(row),
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


def get_unpublished(domain: str, pub_conn: sqlite3.Connection
                    ) -> tuple[list[dict], list[dict]]:
    """Unpublished rows from a domain DB, split into observations and failures.

    Two lists rather than one, because a failed agent run is now recorded as
    a row and the two must not be conflated: an observation gets published, a
    failure gets reported in the log and never leaves the node.
    """
    db_path = DB_PATHS[domain]
    if not db_path.exists():
        log.warning("DB not found for domain '%s': %s", domain, db_path)
        return [], []

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
        return [], []

    unpublished = []
    failures = []
    for row in rows:
        source_id = row["source_id"]
        if already_published(pub_conn, domain, source_id):
            continue
        # A failed run is recorded as a row so the gap has a cause attached,
        # but it is not an observation and must never be published: it holds
        # no assessment, and publishing it would put "Agent run failed" into
        # the lexicon as if it were a reading. It is surfaced in the log
        # instead — see publish_domain.
        if _is_failed_row(row):
            failures.append(dict(row) | {"_source_id": source_id})
            continue
        unpublished.append(dict(row) | {"_source_id": source_id})

    return unpublished, failures


def _is_failed_row(row) -> bool:
    """Whether a domain row records a failed agent run.

    NULL status means a row written before agent_runtime added the column,
    which is a successful run by definition: before it existed, a failed run
    wrote nothing at all.
    """
    try:
        return (row["status"] or "").lower() == "failed"
    except (IndexError, KeyError, TypeError):
        return False


def publish_domain(domain: str, session: BlueskySession,
                   pub_conn: sqlite3.Connection, dry_run: bool) -> int:
    unpublished, failures = get_unpublished(domain, pub_conn)

    # Say when a domain is quiet because its agent broke. "Nothing new to
    # publish" was logged identically for a healthy domain with no new
    # observation and for one whose agent had been crashing for two days,
    # which is how the 2026-09-09/10 Weather and watershed outage stayed
    # invisible: this log was the only place it would have shown, and it said
    # the reassuring thing.
    if failures:
        newest = failures[0]
        log.error("[%s] Last agent run FAILED at %s: %s%s",
                  domain, newest.get("observed_at", "unknown"),
                  newest.get("error") or "no reason recorded",
                  f" ({len(failures)} failed run(s) since the last success)"
                  if len(failures) > 1 else "")

    if not unpublished:
        log.info("[%s] Nothing new to publish%s", domain,
                 " — the agent has not completed a run since the failure above"
                 if failures else "")
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
