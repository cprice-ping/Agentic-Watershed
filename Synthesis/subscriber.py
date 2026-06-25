"""
ATProto Subscriber
------------------
Fetches environmental monitoring observations published by trusted nodes
and stores them in a local SQLite database for the synthesis agent.

Default mode: fetch (cron-friendly, JIT, no persistent connection)
  Calls com.atproto.repo.listRecords for each trusted publisher DID,
  stores any new records, and exits. Run this just before the synthesis
  agent on the same cron schedule.

Optional mode: --firehose (live stream, must be running at publish time)
  Connects to the ATProto firehose and collects records as they arrive.
  Useful for low-latency setups or testing, but architecturally inconsistent
  with the cron-triggered node agents.

Architecture:
  Node (Pi) → ATProto PDS ← [fetch mode: pulls on demand]
                           → Firehose → [firehose mode: live stream]
                                              ↓
                                        subscriber.db
                                              ↓
                                      synthesis agent

Usage:
  python subscriber.py                  # fetch from all trusted publishers, exit
  python subscriber.py --lookback 48    # fetch records from last 48h (default 24)
  python subscriber.py --firehose       # live firehose mode (legacy)
  python subscriber.py --firehose --once --timeout 120  # firehose for 2 min

Cron (run just before synthesis agent):
  50 5,17 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python subscriber.py >> logs/subscriber.log 2>&1
"""

import json
import logging
import os
import sqlite3
import time
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUBSCRIBER_DB = Path(__file__).parent / "data" / "subscriber.db"

LEXICON = "net.cpricedomain.temp.monitor.observation"

# Trusted publisher registry — loaded from publishers.json.
# Azure: edit /data/publishers.json on the File Share (no image rebuild needed).
# Local: edit Synthesis/publishers.json in the repo.
_DATA_DIR         = Path(os.environ.get("DATA_DIR", "/data"))
_PUBLISHERS_PATH  = (
    _DATA_DIR / "publishers.json"
    if (_DATA_DIR / "publishers.json").exists()
    else Path(__file__).parent / "publishers.json"
)
TRUSTED_PUBLISHERS: dict[str, str] = (
    json.loads(_PUBLISHERS_PATH.read_text())
    if _PUBLISHERS_PATH.exists()
    else {"did:plc:demqbviei2gxjjq2eqnm2rpi": "napa-node-01"}
)

PDS_HOST = "https://bsky.social"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("atproto.subscriber")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at     TEXT NOT NULL,
            at_uri          TEXT NOT NULL UNIQUE,
            publisher_did   TEXT NOT NULL,
            node_id         TEXT,
            observed_at     TEXT,
            observation_type TEXT,
            summary         TEXT,
            flagged         INTEGER DEFAULT 0,
            flag_reason     TEXT,
            agent_model     TEXT,
            raw_record      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_obs_type_time
            ON observations (observation_type, received_at DESC);

        CREATE INDEX IF NOT EXISTS idx_obs_publisher
            ON observations (publisher_did, received_at DESC);
    """)
    conn.commit()
    return conn


def store_observation(conn: sqlite3.Connection, at_uri: str,
                      publisher_did: str, record: dict) -> bool:
    """Store a received observation. Returns True if newly inserted."""
    now = datetime.now(timezone.utc).isoformat()
    node_id = TRUSTED_PUBLISHERS.get(publisher_did, "unknown")

    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO observations (
                received_at, at_uri, publisher_did, node_id,
                observed_at, observation_type, summary, flagged,
                flag_reason, agent_model, raw_record
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, at_uri, publisher_did, node_id,
                record.get("observedAt"),
                record.get("observationType", "").split("#")[-1],
                record.get("summary"),
                int(record.get("flagged", False)),
                record.get("flagReason", ""),
                record.get("agentModel"),
                json.dumps(record),
            ),
        )
        conn.commit()
        return conn.execute("SELECT changes()").fetchone()[0] > 0
    except sqlite3.Error as exc:
        log.error("Failed to store observation %s: %s", at_uri, exc)
        return False


def log_record(record: dict, node: str, is_new: bool) -> None:
    if not is_new:
        return
    obs_type = record.get("observationType", "").split("#")[-1]
    flagged = " ⚠️ FLAGGED" if record.get("flagged") else ""
    log.info("Stored [%s] from %s: %s%s",
             obs_type, node, (record.get("summary") or "")[:80], flagged)


# ---------------------------------------------------------------------------
# Fetch mode (default) — pull from PDS via listRecords
# ---------------------------------------------------------------------------

def fetch_from_publisher(conn: sqlite3.Connection, did: str, node_id: str,
                         lookback_hours: float) -> tuple[int, int]:
    """
    Fetch records for a single trusted publisher via com.atproto.repo.listRecords.
    Returns (fetched, stored) counts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    fetched = stored = 0
    cursor = None

    log.info("Fetching records from %s (%s)...", node_id, did)

    with httpx.Client(timeout=30) as client:
        while True:
            params = {
                "repo": did,
                "collection": LEXICON,
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor

            resp = client.get(f"{PDS_HOST}/xrpc/com.atproto.repo.listRecords",
                              params=params)
            resp.raise_for_status()
            data = resp.json()

            records = data.get("records", [])
            if not records:
                break

            for item in records:
                record = item.get("value", {})
                at_uri = item.get("uri", "")

                # Stop if we've gone past the lookback window
                observed_at_str = record.get("observedAt", "")
                if observed_at_str:
                    try:
                        observed_at = datetime.fromisoformat(
                            observed_at_str.replace("Z", "+00:00")
                        )
                        if observed_at < cutoff:
                            return fetched, stored
                    except ValueError:
                        pass

                fetched += 1
                is_new = store_observation(conn, at_uri, did, record)
                if is_new:
                    stored += 1
                    log_record(record, node_id, is_new=True)

            cursor = data.get("cursor")
            if not cursor:
                break

    return fetched, stored


def run_fetch(conn: sqlite3.Connection, lookback_hours: float) -> None:
    """Fetch from all trusted publishers and exit."""
    log.info("=== ATProto Subscriber (fetch mode) ===")
    log.info("Trusted publishers: %s", list(TRUSTED_PUBLISHERS.values()))
    log.info("Lexicon: %s  |  Lookback: %.0fh", LEXICON, lookback_hours)

    total_fetched = total_stored = 0
    for did, node_id in TRUSTED_PUBLISHERS.items():
        try:
            fetched, stored = fetch_from_publisher(conn, did, node_id, lookback_hours)
            total_fetched += fetched
            total_stored += stored
            log.info("%s: %d fetched, %d new", node_id, fetched, stored)
        except httpx.HTTPError as exc:
            log.error("Failed to fetch from %s: %s", node_id, exc)

    log.info("=== Fetch complete — %d fetched, %d new ===", total_fetched, total_stored)


# ---------------------------------------------------------------------------
# Firehose mode (--firehose) — live stream
# ---------------------------------------------------------------------------

def run_firehose(conn: sqlite3.Connection, stop_after: float = None) -> None:
    """Connect to the ATProto firehose and collect records until stopped."""
    from atproto import CAR, FirehoseSubscribeReposClient, parse_subscribe_repos_message
    from atproto import models as atproto_models

    log.info("=== ATProto Subscriber (firehose mode) ===")
    log.info("Trusted publishers: %s", list(TRUSTED_PUBLISHERS.values()))
    log.info("Lexicon: %s", LEXICON)
    if stop_after:
        log.info("Running for %.0f seconds then stopping", stop_after)

    start_time = time.time()

    def on_message(message) -> None:
        if stop_after and (time.time() - start_time) > stop_after:
            raise StopIteration("Time limit reached")

        commit = parse_subscribe_repos_message(message)
        if not isinstance(commit, atproto_models.ComAtprotoSyncSubscribeRepos.Commit):
            return

        publisher_did = commit.repo
        if publisher_did not in TRUSTED_PUBLISHERS:
            return
        if not commit.blocks:
            return

        try:
            car = CAR.from_bytes(commit.blocks)
        except Exception:
            return

        for op in commit.ops:
            if op.action != "create":
                continue
            if not op.path.startswith(LEXICON + "/"):
                continue

            record_cid = op.cid
            if record_cid not in car.blocks:
                continue

            record = car.blocks[record_cid]
            if not isinstance(record, dict):
                continue
            if record.get("$type") != LEXICON:
                continue

            rkey = op.path.split("/")[-1]
            at_uri = f"at://{publisher_did}/{LEXICON}/{rkey}"
            node_id = TRUSTED_PUBLISHERS.get(publisher_did, publisher_did[:16])
            is_new = store_observation(conn, at_uri, publisher_did, record)
            log_record(record, node_id, is_new)

    client = FirehoseSubscribeReposClient()

    def on_error(error: BaseException) -> None:
        if isinstance(error, StopIteration):
            log.info("Time limit reached, stopping")
            client.stop()
        else:
            log.error("Firehose error: %s", error)

    try:
        client.start(on_message, on_error)
    except KeyboardInterrupt:
        log.info("Interrupted, stopping")
        client.stop()

    log.info("=== Firehose subscriber stopped ===")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ATProto subscriber — fetch mode (default) or firehose mode (--firehose)"
    )
    parser.add_argument(
        "--firehose", action="store_true",
        help="Use live firehose instead of fetch (must be running when nodes publish)",
    )
    parser.add_argument(
        "--lookback", type=float, default=24.0,
        help="Hours of history to fetch in fetch mode (default 24)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="[firehose mode] Stop after --timeout seconds",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="[firehose mode] Seconds to run with --once (default 60)",
    )
    parser.add_argument(
        "--db", type=Path, default=SUBSCRIBER_DB,
        help=f"Path to subscriber DB (default: {SUBSCRIBER_DB})",
    )
    args = parser.parse_args()

    conn = init_db(args.db)

    if args.firehose:
        stop_after = args.timeout if args.once else None
        run_firehose(conn, stop_after=stop_after)
    else:
        run_fetch(conn, lookback_hours=args.lookback)


if __name__ == "__main__":
    main()
