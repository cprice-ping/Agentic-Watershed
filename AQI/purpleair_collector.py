"""
PurpleAir collector — valley-floor PM2.5 from low-cost sensors
---------------------------------------------------------------
There is no regulatory PM2.5 monitor in Napa Valley: AirNow answers from
Vallejo, Fairfield or Sebastopol depending on where you ask (CONTEXT.md, "What
a node is"). PurpleAir's network covers the valley floor. This collector reads
it — phase 1 of two:

  Phase 1 (this file): find sensors, poll them, store raw and corrected values,
  and measure what it costs in API points. Nothing reaches the agent yet.

  Phase 2 (not built): wire it into the AQI agent, after three checks — the
  centerline confirmed on a map, PurpleAir's data terms read (they decide
  whether readings or only conclusions can be published), and EPA's
  high-concentration correction taken from the paper rather than memory.

Two calls, because the API bills roughly one point per field per sensor
(measured 2026-10-08: 2,194 points for 199 sensors x 10 fields):

  --discover   once a day: an area search for outdoor sensors, a corridor
               test against the valley centerline, then at most two sensors
               per 3 km along it, so the roster covers the valley's length
               instead of its densest town. Static
               fields (name, position, altitude) are fetched only here.
  (default)    hourly: the roster's sensors by ID, four fields.

What the stored values are, and are not:
  - pm_a / pm_b / humidity are PurpleAir's raw cf_1 channels and the sensor's
    own humidity. A reading whose channels disagree, or with no humidity, or
    from a sensor not reporting in the last hour, is stored with the reason it
    was excluded, never silently dropped.
  - corrected is EPA's US-wide correction (Barkjohn et al., 2021):
        PM2.5 = 0.524 x PA_cf1 - 0.0862 x RH + 5.75
    EPA applies it up to 343 ug/m3 of raw input and a different fit above.
    That extension's coefficients have not been verified here, so above 343
    the raw value is kept and corrected is NULL — heavy smoke is exactly where
    a misremembered coefficient would do the most harm.
  - Values are ug/m3, not AQI, and not comparable as numbers with AirNow's
    nowcastAQI.

Usage:
  python purpleair_collector.py              # poll the roster (discovers first if empty)
  python purpleair_collector.py --discover   # rebuild the roster
  python purpleair_collector.py --summary    # zone medians from the latest poll
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent / "data" / "aqi.db"
API_BASE = "https://api.purpleair.com/v1"

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
PA_CFG = _NODE_CFG["aqi"]["purpleair"]

DISCOVERY_FIELDS = ["name", "latitude", "longitude", "altitude", "location_type",
                    "last_seen"]
POLL_FIELDS = ["last_seen", "humidity", "pm2.5_cf_1_a", "pm2.5_cf_1_b"]

# Rough guide for the logged estimate; the dashboard is the real figure.
# Measured 2026-10-08: discovery 1,229 points for 204 sensors x 6 fields,
# a poll 160 for 31 x 4 — not one clean per-field rate, so this splits the
# difference.
POINTS_PER_FIELD_SENSOR = 1.1

# Channel agreement. These thresholds are this project's choice, not EPA's:
# a reading is excluded only when the channels differ by more than both, so
# small absolute differences at near-zero concentrations still pass. EPA's own
# cleaning rules are in Barkjohn et al. 2021 and should replace these once
# checked.
AB_MAX_ABS_DIFF = 5.0     # ug/m3
AB_MAX_REL_DIFF = 0.70    # of the channel mean

# EPA applies the US-wide correction up to this raw cf_1 concentration.
CORRECTION_MAX_RAW = 343.0

# A sensor whose last report is older than this is not current.
REPORT_MAX_AGE = timedelta(hours=1)

# Roster quality, applied at discovery from readings already stored — no
# extra API cost. Over the last QUALITY_WINDOW, a sensor is left off the
# roster if most of its polls had disagreeing channels (with at least
# QUALITY_MIN_POLLS to judge from), or if its latest reading had no
# humidity. Both faults persist: on 2026-10-08, 5 of 31 roster sensors had
# disagreeing channels and 2 reported no humidity, each sitting in a bin
# capped at two and costing points every poll for nothing. A dropped sensor
# stays a candidate: once its readings age out of the window it is polled
# again, and comes back if it has been fixed.
QUALITY_WINDOW = timedelta(hours=48)
QUALITY_MIN_POLLS = 3
QUALITY_MAX_DISAGREE_SHARE = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("aqi.purpleair")

# The key travels in a header, not the URL, but httpx's request line is still
# not something these logs need — and the two collectors that did put keys in
# URLs leaked them for months (2026-10-08).
logging.getLogger("httpx").setLevel(logging.WARNING)


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
        CREATE TABLE IF NOT EXISTS pa_sensors (
            sensor_index     INTEGER PRIMARY KEY,
            name             TEXT,
            latitude         REAL,
            longitude        REAL,
            altitude         REAL,           -- feet, as PurpleAir reports it
            zone             TEXT,
            corridor_km      REAL,           -- distance from the valley centerline
            along_km         REAL,           -- position along it from the south end
            on_roster        INTEGER NOT NULL DEFAULT 0,
            first_seen_at    TEXT,
            last_discovered  TEXT
        );

        CREATE TABLE IF NOT EXISTS pa_readings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at     TEXT NOT NULL,  -- ISO8601 UTC, when we polled
            sensor_index     INTEGER NOT NULL,
            zone             TEXT,
            last_seen        TEXT,           -- the sensor's own last report, UTC
            pm_a             REAL,           -- pm2.5_cf_1_a, ug/m3, raw
            pm_b             REAL,           -- pm2.5_cf_1_b, ug/m3, raw
            humidity         REAL,           -- sensor's own RH, %
            pm_cf1           REAL,           -- mean of agreeing channels, raw
            corrected        REAL,           -- EPA US-wide 2021; NULL if not applicable
            correction       TEXT,           -- which correction produced `corrected`
            excluded_reason  TEXT            -- NULL when usable; "category: detail"
        );

        CREATE INDEX IF NOT EXISTS idx_pa_readings_time
            ON pa_readings (collected_at DESC);

        CREATE TABLE IF NOT EXISTS pa_calls (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            called_at        TEXT NOT NULL,
            kind             TEXT NOT NULL,  -- 'discover' or 'poll'
            sensors_returned INTEGER,
            fields           INTEGER,
            points_estimate  INTEGER,        -- estimate, see POINTS_PER_FIELD_SENSOR
            status           TEXT NOT NULL,  -- 'ok' or 'failed'
            detail           TEXT
        );
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(pa_sensors)")}
    if "roster_note" not in have:
        # Why a corridor sensor is not on the roster, when the reason is its
        # own record rather than the per-bin cap. NULL otherwise.
        conn.execute("ALTER TABLE pa_sensors ADD COLUMN roster_note TEXT")
    conn.commit()


def sensor_faults(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """{sensor_index: reason} for sensors whose recent record disqualifies
    them from the roster. See QUALITY_WINDOW."""
    now = now or datetime.now(timezone.utc)
    since = (now - QUALITY_WINDOW).isoformat()
    rows = conn.execute(
        "SELECT sensor_index, collected_at, excluded_reason FROM pa_readings "
        "WHERE collected_at >= ? ORDER BY sensor_index, collected_at",
        (since,)).fetchall()
    by_sensor: dict[int, list] = {}
    for r in rows:
        by_sensor.setdefault(r["sensor_index"], []).append(r["excluded_reason"] or "")
    faults = {}
    for idx, reasons in by_sensor.items():
        if reasons[-1].startswith("no humidity"):
            faults[idx] = "latest reading had no humidity"
            continue
        disagree = sum(r.startswith("channels disagree") for r in reasons)
        if (len(reasons) >= QUALITY_MIN_POLLS
                and disagree / len(reasons) > QUALITY_MAX_DISAGREE_SHARE):
            faults[idx] = (f"channels disagreed on {disagree} of "
                           f"{len(reasons)} polls in {QUALITY_WINDOW.total_seconds() / 3600:g}h")
    return faults


# ---------------------------------------------------------------------------
# Geometry — distance from the valley centerline
# ---------------------------------------------------------------------------

def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Kilometres on a local flat projection. Accurate to well under 1% over
    a valley 50 km long, which is plenty for a 3 km corridor test."""
    return (lon * 111.320 * math.cos(math.radians(lat0)), lat * 110.574)


def place_on_line(lat: float, lon: float, line: list) -> tuple[float, float]:
    """(distance from the polyline, distance along it from its first point),
    both in km."""
    lat0 = sum(p[0] for p in line) / len(line)
    px, py = _xy(lat, lon, lat0)
    best = (float("inf"), 0.0)
    walked = 0.0
    for (alat, alon, *_), (blat, blon, *_) in zip(line, line[1:]):
        ax, ay = _xy(alat, alon, lat0)
        bx, by = _xy(blat, blon, lat0)
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg ** 2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, walked + t * seg)
        walked += seg
    return best


def zone_for(along_km: float, line: list, zones: list) -> str | None:
    """The configured zone containing this position along the line."""
    lat0 = sum(p[0] for p in line) / len(line)
    at, walked = {}, 0.0
    at[line[0][2]] = 0.0
    for a, b in zip(line, line[1:]):
        ax, ay = _xy(a[0], a[1], lat0)
        bx, by = _xy(b[0], b[1], lat0)
        walked += math.hypot(bx - ax, by - ay)
        at[b[2]] = walked
    for z in zones:
        if at[z["from"]] <= along_km <= at[z["to"]]:
            return z["name"]
    return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _key() -> str:
    key = os.environ.get("PURPLEAIR_API_KEY", "").strip()
    if not key:
        raise RuntimeError("PURPLEAIR_API_KEY is not set (.env)")
    return key


def _get(params: dict) -> list[dict]:
    """GET /sensors and return rows as dicts keyed by the response's own field
    list — never by position, which the API does not promise."""
    resp = httpx.get(f"{API_BASE}/sensors", params=params,
                     headers={"X-API-Key": _key()}, timeout=30)
    if resp.status_code != 200:
        # PurpleAir's error body names the problem (a bad field, an unknown
        # parameter); keep it, it is the whole diagnosis.
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    fields = body.get("fields") or []
    return [dict(zip(fields, row)) for row in body.get("data") or []]


def _record_call(conn, kind, rows, n_fields, status, detail=None):
    n = len(rows) if rows is not None else None
    est = round(n * n_fields * POINTS_PER_FIELD_SENSOR) if n is not None else None
    conn.execute(
        "INSERT INTO pa_calls (called_at, kind, sensors_returned, fields, "
        "points_estimate, status, detail) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), kind, n, n_fields, est,
         status, detail))
    conn.commit()
    if est is not None:
        log.info("%s: %d sensor(s) x %d field(s), ~%d points (estimate)",
                 kind, n, n_fields, est)


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Discovery — which sensors are on the valley floor
# ---------------------------------------------------------------------------

def discover(conn: sqlite3.Connection) -> int:
    nwlng, nwlat, selng, selat = PA_CFG["discovery_bbox"]
    params = {
        "fields": ",".join(DISCOVERY_FIELDS),
        "location_type": 0,                 # outdoor
        "max_age": 86400,                   # seen in the last day
        "nwlng": nwlng, "nwlat": nwlat, "selng": selng, "selat": selat,
    }
    try:
        rows = _get(params)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("PurpleAir discovery failed: %s", exc)
        _record_call(conn, "discover", None, len(DISCOVERY_FIELDS), "failed", str(exc))
        return 0
    _record_call(conn, "discover", rows, len(DISCOVERY_FIELDS), "ok")

    line, zones = PA_CFG["centerline"], PA_CFG["zones"]
    width = float(PA_CFG["corridor_km"])
    now = datetime.now(timezone.utc).isoformat()
    candidates: dict[str, list] = {}
    for r in rows:
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is None or lon is None or r.get("location_type") not in (0, None):
            continue
        off, along = place_on_line(float(lat), float(lon), line)
        zone = zone_for(along, line, zones) if off <= width else None
        conn.execute(
            """INSERT INTO pa_sensors (sensor_index, name, latitude, longitude,
                   altitude, zone, corridor_km, along_km, on_roster,
                   first_seen_at, last_discovered)
               VALUES (?,?,?,?,?,?,?,?,0,?,?)
               ON CONFLICT(sensor_index) DO UPDATE SET
                   name=excluded.name, latitude=excluded.latitude,
                   longitude=excluded.longitude, altitude=excluded.altitude,
                   zone=excluded.zone, corridor_km=excluded.corridor_km,
                   along_km=excluded.along_km, on_roster=0,
                   last_discovered=excluded.last_discovered""",
            (r["sensor_index"], r.get("name"), lat, lon, r.get("altitude"),
             zone, round(off, 2), round(along, 2), now, now))
        if zone:
            candidates.setdefault(zone, []).append((off, along, r["sensor_index"]))

    # Spread along the valley, not the densest cluster. Sensors cluster where
    # people live, so "nearest the centerline first" filled the roster with
    # downtown Napa and left the stretches between towns empty — and
    # coverage along the valley's length is the whole point: smoke pooling in
    # one stretch is invisible to ten sensors in another. Each bin keeps at
    # most max_per_bin sensors, nearest the floor first; two, so each spot
    # has a neighbour to expose a faulty sensor. The cap also bounds the
    # poll's cost, since points scale with sensors.
    #
    # Sensors with a persistent fault are skipped and the slot goes to the
    # next nearest in the same bin (sensor_faults).
    bin_km = float(PA_CFG["bin_km"])
    per_bin = int(PA_CFG["max_per_bin"])
    faults = sensor_faults(conn)
    conn.execute("UPDATE pa_sensors SET roster_note = NULL")
    roster = []
    for zone, items in sorted(candidates.items()):
        bins: dict[int, list] = {}
        for off, along, idx in items:
            bins.setdefault(int(along // bin_km), []).append((off, idx))
        chosen, skipped = [], 0
        for b in sorted(bins):
            healthy = []
            for _, idx in sorted(bins[b]):
                if idx in faults:
                    skipped += 1
                    conn.execute("UPDATE pa_sensors SET roster_note=? WHERE sensor_index=?",
                                 (faults[idx], idx))
                else:
                    healthy.append(idx)
            chosen.extend(healthy[:per_bin])
        roster.extend(chosen)
        log.info("  %s: %d candidate(s) in the corridor, %d skipped for their "
                 "record, %d on the roster across %d bin(s)",
                 zone, len(items), skipped, len(chosen), len(bins))
    conn.executemany("UPDATE pa_sensors SET on_roster=1 WHERE sensor_index=?",
                     [(i,) for i in roster])
    conn.commit()
    log.info("Discovery: %d sensor(s) in the area, %d on the roster",
             len(rows), len(roster))
    return len(roster)


# ---------------------------------------------------------------------------
# Poll — current readings for the roster
# ---------------------------------------------------------------------------

def correct(pm_cf1: float, rh: float) -> tuple[float | None, str | None]:
    """EPA US-wide correction (Barkjohn et al. 2021), within its range.

    Floored at 0: the linear fit goes slightly negative at near-zero
    concentration and high humidity, and a negative concentration is not a
    measurement. Above CORRECTION_MAX_RAW, None — see the module docstring.
    """
    if pm_cf1 > CORRECTION_MAX_RAW:
        return None, None
    return max(0.0, 0.524 * pm_cf1 - 0.0862 * rh + 5.75), "epa_us_2021"


def assess(r: dict, now: datetime) -> dict:
    """A stored reading for one sensor row, with an exclusion reason if it is
    not usable. Pure, so it can be tested without the API."""
    a, b, rh = r.get("pm2.5_cf_1_a"), r.get("pm2.5_cf_1_b"), r.get("humidity")
    seen = _iso(r.get("last_seen"))
    out = {"last_seen": seen, "pm_a": a, "pm_b": b, "humidity": rh,
           "pm_cf1": None, "corrected": None, "correction": None,
           "excluded_reason": None}
    seen_at = datetime.fromisoformat(seen) if seen else None
    if seen_at is None or now - seen_at > REPORT_MAX_AGE:
        out["excluded_reason"] = "not reporting in the last hour"
    elif a is None or b is None:
        out["excluded_reason"] = "channel missing: agreement can't be checked"
    elif rh is None:
        out["excluded_reason"] = "no humidity: the correction can't be applied"
    else:
        diff, mean = abs(a - b), (a + b) / 2.0
        if diff > AB_MAX_ABS_DIFF and mean > 0 and diff / mean > AB_MAX_REL_DIFF:
            out["excluded_reason"] = f"channels disagree: A {a:g}, B {b:g}"
        else:
            out["pm_cf1"] = round(mean, 2)
            c, how = correct(mean, float(rh))
            out["corrected"] = None if c is None else round(c, 1)
            out["correction"] = how
            if c is None:
                out["excluded_reason"] = (
                    f"above validated range: raw {mean:g} ug/m3 > "
                    f"{CORRECTION_MAX_RAW:g}; raw value kept")
    return out


def poll(conn: sqlite3.Connection) -> int:
    roster = {row["sensor_index"]: row["zone"] for row in conn.execute(
        "SELECT sensor_index, zone FROM pa_sensors WHERE on_roster=1")}
    if not roster:
        log.info("No roster yet; running discovery first")
        if not discover(conn):
            return 0
        roster = {row["sensor_index"]: row["zone"] for row in conn.execute(
            "SELECT sensor_index, zone FROM pa_sensors WHERE on_roster=1")}

    params = {"fields": ",".join(POLL_FIELDS),
              "show_only": ",".join(str(i) for i in sorted(roster))}
    try:
        rows = _get(params)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("PurpleAir poll failed: %s", exc)
        _record_call(conn, "poll", None, len(POLL_FIELDS), "failed", str(exc))
        return 0
    _record_call(conn, "poll", rows, len(POLL_FIELDS), "ok")

    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    usable = 0
    returned = set()
    for r in rows:
        idx = r.get("sensor_index")
        returned.add(idx)
        a = assess(r, now)
        usable += a["excluded_reason"] is None
        conn.execute(
            """INSERT INTO pa_readings (collected_at, sensor_index, zone,
                   last_seen, pm_a, pm_b, humidity, pm_cf1, corrected,
                   correction, excluded_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (stamp, idx, roster.get(idx), a["last_seen"], a["pm_a"], a["pm_b"],
             a["humidity"], a["pm_cf1"], a["corrected"], a["correction"],
             a["excluded_reason"]))
    # A roster sensor the API did not return at all is recorded too: absent
    # is a different fact from "reported nothing usable".
    for idx in sorted(set(roster) - returned):
        conn.execute(
            "INSERT INTO pa_readings (collected_at, sensor_index, zone, "
            "excluded_reason) VALUES (?,?,?,?)",
            (stamp, idx, roster[idx], "not returned by the API"))
    conn.commit()
    log.info("Poll: %d of %d roster sensor(s) usable", usable, len(roster))
    return usable


# ---------------------------------------------------------------------------
# Summary — for checking phase 1 by eye
# ---------------------------------------------------------------------------

def summary(conn: sqlite3.Connection) -> dict:
    last = conn.execute("SELECT MAX(collected_at) AS t FROM pa_readings").fetchone()["t"]
    if not last:
        return {"status": "no polls yet"}
    rows = conn.execute(
        "SELECT zone, sensor_index, corrected, pm_cf1, excluded_reason "
        "FROM pa_readings WHERE collected_at = ?", (last,)).fetchall()
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(last)).total_seconds() / 3600.0
    zones = {}
    for z in sorted({r["zone"] or "?" for r in rows}):
        zr = [r for r in rows if (r["zone"] or "?") == z]
        ok = [r["corrected"] for r in zr if r["excluded_reason"] is None]
        reasons: dict[str, int] = {}
        for r in zr:
            if r["excluded_reason"]:
                # Every reason starts with its category; details follow a colon.
                key = r["excluded_reason"].split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
        zones[z] = {
            "median_corrected_ug_m3": round(statistics.median(ok), 1) if ok else None,
            "sensors_used": len(ok),
            "sensors_on_roster": len(zr),
            "excluded": reasons,
        }
    calls = conn.execute(
        "SELECT kind, COUNT(*) n, SUM(points_estimate) pts FROM pa_calls "
        "WHERE called_at >= ? AND status = 'ok' GROUP BY kind",
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),)).fetchall()
    return {
        "latest_poll_utc": last,
        "latest_poll_age_hours": round(age_h, 1),
        "units": "ug/m3, EPA US-wide correction (2021); not AQI",
        "zones": zones,
        "points_estimate_last_24h": {r["kind"]: {"calls": r["n"], "points": r["pts"]}
                                     for r in calls},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="PurpleAir valley-floor collector")
    ap.add_argument("--discover", action="store_true", help="Rebuild the roster")
    ap.add_argument("--summary", action="store_true", help="Zone medians, latest poll")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = get_db(Path(args.db))
    init_db(conn)
    if args.summary:
        print(json.dumps(summary(conn), indent=2))
    elif args.discover:
        if not discover(conn):
            sys.exit(1)
    elif not poll(conn):
        sys.exit(1)


if __name__ == "__main__":
    main()
