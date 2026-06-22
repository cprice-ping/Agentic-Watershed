"""
ATProto Firehose Subscriber
---------------------------
Long-running daemon that subscribes to the ATProto firehose and collects
environmental monitoring observations published by trusted nodes.

Filters by:
  1. Publisher DID — only records from the trusted registry
  2. Lexicon NSID — only net.cpricedomain.temp.monitor.observation records

Stores received observations to a local SQLite database that the
synthesis agent reads for cross-domain reasoning.

This replaces the direct SQLite reads in synthesis/agent.py — instead of
reading domain agent databases directly, the synthesis agent reads this
subscriber database, which is populated from ATProto records.

This is the decoupling step: synthesis can now run anywhere that can reach
the firehose. The nodes it trusts are defined in the trusted registry,
not by filesystem paths.

Architecture:
  Node (Pi) → ATProto PDS → Firehose → [this subscriber] → subscriber.db
                                                                   ↓
                                                        Synthesis agent reads

Run as a daemon:
  python subscriber.py           # runs until stopped
  python subscriber.py --once    # process for 60s then exit (for testing)

Systemd service (recommended for production):
  See subscriber.service in this directory
"""

import json
import logging
import sqlite3
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

from atproto import CAR, FirehoseSubscribeReposClient, parse_subscribe_repos_message
from atproto import models as atproto_models

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUBSCRIBER_DB = Path(__file__).parent / "data" / "subscriber.db"

LEXICON = "net.cpricedomain.temp.monitor.observation"

# Trusted publisher registry — DIDs we will accept observations from
# Add new node DIDs here as the network grows
TRUSTED_PUBLISHERS = {
    "did:plc:demqbviei2gxjjq2eqnm2rpi": "napa-node-01",  # napanode1.bsky.social
    # "did:plc:...": "napa-node-02",  # add when node-02 is created
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("atproto.subscriber")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    SUBSCRIBER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SUBSCRIBER_DB)
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
            raw_record      TEXT        -- full JSON record for future use
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
                now,
                at_uri,
                publisher_did,
                node_id,
                record.get("observedAt"),
                record.get("observationType", "").split("#")[-1],  # "watershed" etc
                record.get("summary"),
                int(record.get("flagged", False)),
                record.get("flagReason", ""),
                record.get("agentModel"),
                json.dumps(record),
            ),
        )
        conn.commit()
        return conn.execute(
            "SELECT changes()"
        ).fetchone()[0] > 0
    except sqlite3.Error as exc:
        log.error("Failed to store observation %s: %s", at_uri, exc)
        return False


# ---------------------------------------------------------------------------
# Firehose message handler
# ---------------------------------------------------------------------------

def make_handler(conn: sqlite3.Connection, stop_after: float = None):
    """Returns a firehose message handler. stop_after: seconds to run (None = forever)."""
    start_time = time.time()
    processed = 0
    accepted = 0

    def on_message(message) -> None:
        nonlocal processed, accepted

        # Stop after timeout if set
        if stop_after and (time.time() - start_time) > stop_after:
            raise StopIteration("Time limit reached")

        commit = parse_subscribe_repos_message(message)

        # Only process commit events
        if not isinstance(commit, atproto_models.ComAtprotoSyncSubscribeRepos.Commit):
            return

        # Quick check: is this DID in our trusted registry?
        publisher_did = commit.repo
        if publisher_did not in TRUSTED_PUBLISHERS:
            return

        # Decode the CAR blocks
        if not commit.blocks:
            return

        try:
            car = CAR.from_bytes(commit.blocks)
        except Exception:
            return

        # Look for our lexicon records in the operations
        for op in commit.ops:
            if op.action != "create":
                continue

            # Check if this operation is in our lexicon collection
            if not op.path.startswith(LEXICON + "/"):
                continue

            processed += 1

            # Find the record in the CAR blocks
            record_cid = op.cid
            if record_cid not in car.blocks:
                continue

            record = car.blocks[record_cid]
            if not isinstance(record, dict):
                continue

            # Verify it's actually our lexicon type
            if record.get("$type") != LEXICON:
                continue

            # Build AT URI
            rkey = op.path.split("/")[-1]
            at_uri = f"at://{publisher_did}/{LEXICON}/{rkey}"

            # Store it
            is_new = store_observation(conn, at_uri, publisher_did, record)
            if is_new:
                accepted += 1
                obs_type = record.get("observationType", "").split("#")[-1]
                flagged = "⚠️ FLAGGED" if record.get("flagged") else ""
                node = TRUSTED_PUBLISHERS.get(publisher_did, publisher_did[:16])
                log.info(
                    "Received [%s] from %s: %s %s",
                    obs_type, node,
                    (record.get("summary") or "")[:80],
                    flagged,
                )

    return on_message


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ATProto firehose subscriber")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run for 60 seconds then exit (for testing)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds when --once is used (default 60)",
    )
    args = parser.parse_args()

    conn = init_db()
    log.info("=== ATProto Subscriber starting ===")
    log.info("Trusted publishers: %s", list(TRUSTED_PUBLISHERS.values()))
    log.info("Lexicon filter: %s", LEXICON)

    stop_after = args.timeout if args.once else None
    if args.once:
        log.info("Running in --once mode for %.0f seconds", stop_after)

    handler = make_handler(conn, stop_after=stop_after)

    def on_error(error: BaseException) -> None:
        if isinstance(error, StopIteration):
            log.info("Time limit reached, stopping")
            client.stop()
        else:
            log.error("Firehose error: %s", error)

    client = FirehoseSubscribeReposClient()

    try:
        client.start(handler, on_error)
    except KeyboardInterrupt:
        log.info("Interrupted, stopping")
        client.stop()

    log.info("=== Subscriber stopped ===")


if __name__ == "__main__":
    main()
