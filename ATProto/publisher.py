"""
ATProto Node Publisher
----------------------
Publishes domain agent observations (watershed, weather, aqi) to ATProto
as the node identity (napanode1.bsky.social / napa-node-01 DID).

Domain observations are machine-readable, lexicon-tagged records intended
for other agents to consume via the firehose subscriber. They are NOT
the human-facing advisory — that's the synthesis publisher's job.

Identity: BSKY_HANDLE / BSKY_APP_PASSWORD → node DID (napa-node-01)
          Set in /etc/environment on the Pi.

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
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = Path("/home/cprice/Agentic-Watershed")

DB_PATHS = {
    "watershed": BASE / "Watershed" / "data" / "watershed.db",
    "weather":   BASE / "Weather"   / "data" / "weather.db",
    "aqi":       BASE / "AQI"       / "data" / "aqi.db",
}

# Track what's been published — simple SQLite alongside the publisher
PUBLISHER_DB = Path(__file__).parent / "data" / "publisher.db"

BSKY_PDS = "https://bsky.social"
LEXICON  = "net.cpricedomain.temp.monitor.observation"
NODE_ID  = "napa-node-01"

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

    def create_post(self, text: str, tags: list[str] = None) -> str:
        """Create a Bluesky post (app.bsky.feed.post). Returns AT URI."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Build facets for hashtags
        # ATProto requires UTF-8 byte offsets, not character offsets
        facets = []
        full_text = text
        if tags:
            # Build the full text first, then calculate offsets from it
            tag_parts = [f"#{t}" for t in tags]
            tag_str = " " + " ".join(tag_parts)
            full_text = text + tag_str

            # Calculate byte offsets by scanning the encoded full text
            full_bytes = full_text.encode("utf-8")
            for tag in tags:
                needle = f"#{tag}".encode("utf-8")
                idx = full_bytes.find(needle)
                if idx == -1:
                    continue
                facets.append({
                    "index": {
                        "byteStart": idx,
                        "byteEnd": idx + len(needle),
                    },
                    "features": [{
                        "$type": "app.bsky.richtext.facet#tag",
                        "tag": tag,
                    }],
                })

        record = {
            "$type": "app.bsky.feed.post",
            "text": full_text,
            "createdAt": now,
        }
        if facets:
            record["facets"] = facets

        return self.create_record("app.bsky.feed.post", record)


# ---------------------------------------------------------------------------
# Observation builders — convert DB rows to lexicon records
# ---------------------------------------------------------------------------

def build_watershed_record(row: dict, observed_at: str) -> dict:
    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#watershed",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": "claude-haiku-4-5",
        "watershed": {
            "stationIds": ["USGS-11458000", "USGS-11456000"],
            "sevenDayTrend": "unknown",  # could be derived from DB in future
        },
    }


def build_weather_record(row: dict, observed_at: str) -> dict:
    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#weather",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": "claude-haiku-4-5",
        "weather": {
            "stationId": "KAPC",
            "activeAlerts": [],
        },
    }


def build_aqi_record(row: dict, observed_at: str) -> dict:
    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": NODE_ID,
        "observationType": f"{LEXICON}#aqi",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": "",
        "agentModel": "claude-haiku-4-5",
        "aqi": {
            "reportingArea": "Napa",
        },
    }


# ---------------------------------------------------------------------------
# Post text builders
# ---------------------------------------------------------------------------

def truncate_graphemes(text: str, limit: int) -> str:
    """Truncate text to a grapheme limit. Uses character count as approximation
    (close enough for ASCII-heavy environmental summaries)."""
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def build_post_text(domain: str, row: dict) -> tuple[str, list[str]]:
    """Returns (post_text, hashtags). Post text fits within Bluesky's 300 grapheme limit."""
    summary = row.get("summary", "")
    flagged = bool(row.get("flagged", False))

    domain_labels = {
        "watershed": "🌊 Watershed",
        "weather":   "🌤️ Weather",
        "aqi":       "💨 Air Quality",
    }
    label = domain_labels.get(domain, "📡 Monitor")
    header = f"⚠️ {label} — conditions flagged" if flagged else f"✅ {label} — normal conditions"

    tags = ["NapaValley", "WatershedMonitor"]
    if flagged:
        tags.append("Flagged")

    tag_str = " " + " ".join(f"#{t}" for t in tags)
    overhead = len(header) + 2 + len(tag_str)
    summary = truncate_graphemes(summary, 300 - overhead)

    return f"{header}\n\n{summary}", tags


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
        rows = conn.execute(
            f"SELECT rowid AS source_id, * FROM {config['table']} ORDER BY rowid DESC LIMIT 20"
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
        post_text, tags = build_post_text(domain, row)
        flagged = bool(row.get("flagged", False))

        if dry_run:
            log.info("[%s] [DRY RUN] Would publish observation %d:", domain, source_id)
            log.info("  Post: %s", post_text[:100] + "...")
            log.info("  Flagged: %s  Tags: %s", flagged, tags)
            mark_published(pub_conn, domain, source_id, observed_at, "dry-run", flagged)
            count += 1
            continue

        try:
            # Publish the structured lexicon record
            record_uri = session.create_record(LEXICON, record)
            log.info("[%s] Published lexicon record: %s", domain, record_uri)

            # Publish the human-readable Bluesky post
            post_uri = session.create_post(post_text, tags)
            log.info("[%s] Published post: %s", domain, post_uri)

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
        choices=["all", "watershed", "weather", "aqi"],
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
        ["watershed", "weather", "aqi"]
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

    total = 0
    for domain in domains:
        count = publish_domain(domain, session, pub_conn, dry_run=args.dry_run)
        log.info("[%s] Published %d record(s)", domain, count)
        total += count

    log.info("=== Publisher complete — %d total records published ===", total)


if __name__ == "__main__":
    main()
