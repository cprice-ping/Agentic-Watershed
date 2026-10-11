"""
Node status — one screen, read-only
-----------------------------------
What the logs say piecemeal, in one place: is every collector current, did
every agent's last run succeed, what did the publisher last send, and how is
the PurpleAir phase-1 collection doing. Run inside the node container:

    docker compose exec node python /app/status.py

Read-only by construction: every database is opened with mode=ro, so this
can be run at any time, including while jobs are writing.

Exit status is 0 when everything is within its expected age and no agent has
failed since its last success, 1 otherwise — so the same command can later be
what an alert runs.

The expected ages are the schedule in node.crontab plus slack: a job is
"late" when it has missed about one run, not when it is a minute behind.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent

# (label, database, query for the newest timestamp, late after N hours)
# Each query returns one ISO8601 UTC timestamp, or NULL.
COLLECTORS = [
    ("River collector", "River/data/watershed.db",
     "SELECT MAX(collected_at) FROM readings", 1.0),          # every 15 min
    ("Weather collector", "Weather/data/weather.db",
     "SELECT MAX(collected_at) FROM observations", 1.5),      # every 30 min
    ("AQI collector (AirNow)", "AQI/data/aqi.db",
     "SELECT MAX(collected_at) FROM observations", 1.5),      # every 30 min
    ("Fire collector (FIRMS)", "Fire/data/fire.db",
     "SELECT MAX(polled_at) FROM polls WHERE status = 'ok'", 1.5),
    ("Fire incidents (CAL FIRE)", "Fire/data/fire.db",
     "SELECT MAX(last_seen_at) FROM incidents", 13.0),        # 2x/day
    ("PurpleAir poll", "AQI/data/aqi.db",
     "SELECT MAX(called_at) FROM pa_calls "
     "WHERE kind = 'poll' AND status = 'ok'", 2.5),            # hourly
]

# (label, database, late after N hours) — agents write agent_observations.
AGENTS = [
    ("River agent", "River/data/watershed.db", 13.0),          # 2x/day
    ("Weather agent", "Weather/data/weather.db", 13.0),
    ("AQI agent", "AQI/data/aqi.db", 7.0),                     # 4x/day
    ("Fire agent", "Fire/data/fire.db", 13.0),
]

PUBLISHER_DB = "ATProto/data/publisher.db"
PUBLISHER_LATE_HOURS = 7.0                                     # 4x/day


def _ro(rel: str):
    path = BASE / rel
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse(ts):
    try:
        t = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age(ts, now) -> str:
    t = _parse(ts)
    if t is None:
        return "never"
    h = (now - t).total_seconds() / 3600.0
    return f"{h * 60:.0f}m ago" if h < 1 else f"{h:.1f}h ago"


def _late(ts, now, hours) -> bool:
    t = _parse(ts)
    return t is None or now - t > timedelta(hours=hours)


def collectors(now, problems):
    print("COLLECTORS")
    for label, db, query, late_h in COLLECTORS:
        conn = _ro(db)
        try:
            newest = conn.execute(query).fetchone()[0] if conn else None
        except sqlite3.Error as exc:
            newest = None
            print(f"  {label:28} cannot read: {exc}")
            problems.append(label)
            continue
        finally:
            if conn:
                conn.close()
        late = _late(newest, now, late_h)
        if late:
            problems.append(label)
        print(f"  {label:28} {_age(newest, now):>12}"
              + (f"   LATE (expected within {late_h:g}h)" if late else ""))


def agents(now, problems):
    print("\nAGENTS")
    for label, db, late_h in AGENTS:
        conn = _ro(db)
        if conn is None:
            print(f"  {label:28} no database")
            problems.append(label)
            continue
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_observations)")}
            status = "status" if "status" in cols else "NULL AS status"
            error = "error" if "error" in cols else "NULL AS error"
            model = "model" if "model" in cols else "NULL AS model"
            rows = conn.execute(
                f"SELECT observed_at, {status}, {error}, {model} "
                "FROM agent_observations ORDER BY rowid DESC LIMIT 50").fetchall()
        except sqlite3.Error as exc:
            print(f"  {label:28} cannot read: {exc}")
            problems.append(label)
            continue
        finally:
            conn.close()
        # Failures since the newest success — the same cut-off the publisher
        # uses (#121): older failures are history, already resolved.
        failed_since, last_ok = [], None
        for r in rows:
            if (r["status"] or "").lower() == "failed":
                if last_ok is None:
                    failed_since.append(r)
            elif last_ok is None:
                last_ok = r
        ok_at = last_ok["observed_at"] if last_ok else None
        line = f"  {label:28} last success {_age(ok_at, now):>12}"
        if last_ok and last_ok["model"]:
            line += f"  ({last_ok['model']})"
        if _late(ok_at, now, late_h):
            line += f"   LATE (expected within {late_h:g}h)"
            problems.append(label)
        print(line)
        if failed_since:
            problems.append(label)
            newest = failed_since[0]
            print(f"  {'':28} {len(failed_since)} failure(s) since; newest "
                  f"{_age(newest['observed_at'], now)}: "
                  f"{(newest['error'] or 'no reason recorded')[:160]}")


def publisher(now, problems):
    print("\nPUBLISHER")
    conn = _ro(PUBLISHER_DB)
    if conn is None:
        print("  no database")
        problems.append("publisher")
        return
    try:
        rows = conn.execute(
            "SELECT domain, MAX(published_at) AS last, "
            "SUM(published_at >= ?) AS day "
            "FROM published GROUP BY domain ORDER BY domain",
            ((now - timedelta(days=1)).isoformat(),)).fetchall()
    except sqlite3.Error as exc:
        print(f"  cannot read: {exc}")
        problems.append("publisher")
        return
    finally:
        conn.close()
    newest = max((r["last"] for r in rows if r["last"]), default=None)
    for r in rows:
        print(f"  {r['domain']:28} last published {_age(r['last'], now):>12}"
              f"   {r['day'] or 0} in the last 24h")
    if _late(newest, now, PUBLISHER_LATE_HOURS):
        print(f"  nothing published in {PUBLISHER_LATE_HOURS:g}h   LATE")
        problems.append("publisher")


def purpleair(now):
    # Internal only: PurpleAir's terms allow internal development use, not
    # publication (CONTEXT.md). Shown here because this is not published.
    print("\nPURPLEAIR (phase 1, internal only)")
    sys.path.insert(0, str(BASE / "AQI"))
    try:
        import purpleair_collector as pa
        conn = _ro("AQI/data/aqi.db")
        if conn is None:
            print("  no database")
            return
        try:
            s = pa.summary(conn)
        finally:
            conn.close()
    except Exception as exc:          # a status screen must not die on one section
        print(f"  unavailable: {exc}")
        return
    if "zones" not in s:
        print(f"  {s.get('status', 'no data')}")
        return
    print(f"  latest poll {_age(s['latest_poll_utc'], now)}  ({s['units']})")
    for zone, z in s["zones"].items():
        med = z["median_corrected_ug_m3"]
        excl = ", ".join(f"{k} {v}" for k, v in z["excluded"].items()) or "none"
        print(f"  {zone:14} median {med if med is not None else '-':>5}  "
              f"{z['sensors_used']}/{z['sensors_on_roster']} sensors used; "
              f"excluded: {excl}")
    pts = s.get("points_estimate_last_24h") or {}
    total = sum(v.get("points") or 0 for v in pts.values())
    print(f"  points, last 24h (estimate): ~{total:,}")


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        node = json.loads((BASE / "node_config.json").read_text())["node_id"]
    except (OSError, ValueError, KeyError):
        node = "node"
    print(f"{node} status — {now.strftime('%Y-%m-%d %H:%M UTC')} — "
          f"code {os.environ.get('NODE_CODE_VERSION', 'unknown')}\n")
    problems: list[str] = []
    collectors(now, problems)
    agents(now, problems)
    publisher(now, problems)
    purpleair(now)
    print("\n" + ("OK" if not problems else
                  f"ATTENTION: {', '.join(dict.fromkeys(problems))}"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
