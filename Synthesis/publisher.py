"""
ATProto Synthesis Publisher
---------------------------
Publishes synthesis (cross-domain advisory) observations to ATProto
as the synthesis identity — a separate Bluesky account from the node.

This is a distinct identity from the node publisher (ATProto/publisher.py):

  Node identity    (BSKY_HANDLE / BSKY_APP_PASSWORD)
    → napanode1.bsky.social (napa-node-01)
    → publishes: watershed, weather, aqi domain observations
    → audience: other agents subscribing to the firehose

  Synthesis identity  (BSKY_SYNTH_HANDLE / BSKY_SYNTH_APP_PASSWORD)
    → napasynth01.bsky.social (napa-synth-01)   ← needs creating
    → publishes: cross-domain advisory summaries
    → audience: people who want plain-language risk assessments

Separating these identities means:
  - A person can follow the synthesis account for advisories without
    seeing the raw domain observations
  - The synthesis DID can be moved to a different machine without
    touching the node's credentials
  - Trust is explicit: the synthesis account's DID is a distinct
    identity from the node, with its own key material

Setup required before first run:
  1. Create a Bluesky account for synthesis (e.g. napasynth01.bsky.social)
  2. Generate an App Password in Settings → Privacy → App Passwords
  3. Set BSKY_SYNTH_HANDLE and BSKY_SYNTH_APP_PASSWORD in /etc/environment

Usage:
  python publisher.py           # publish any unpublished synthesis observations
  python publisher.py --dry-run

Cron (run after synthesis agent — 15 min after agent fires):
  15 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python publisher.py >> logs/publisher.log 2>&1
"""

import argparse
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Pi layout:    /Agentic/Synthesis/data/synthesis.db
# Laptop/repo:  Synthesis/agent/data/synthesis.db  (agent writes here when run in-tree)
_DEFAULT_SYNTHESIS_DB = Path(__file__).parent / "agent" / "data" / "synthesis.db"
_DEFAULT_PUBLISHER_DB = Path(__file__).parent / "data" / "synth_publisher.db"

BSKY_PDS  = "https://bsky.social"
LEXICON   = "net.cpricedomain.temp.monitor.observation"
SYNTH_ID  = "napa-synth-01"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("atproto.synth_publisher")


# ---------------------------------------------------------------------------
# Publisher DB
# ---------------------------------------------------------------------------

def init_publisher_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS published (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            published_at TEXT NOT NULL,
            source_id    INTEGER NOT NULL UNIQUE,
            observed_at  TEXT NOT NULL,
            at_uri       TEXT,
            overall_risk TEXT,
            flagged      INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn


def already_published(conn: sqlite3.Connection, source_id: int) -> bool:
    return conn.execute(
        "SELECT id FROM published WHERE source_id = ?", (source_id,)
    ).fetchone() is not None


def mark_published(conn: sqlite3.Connection, source_id: int, observed_at: str,
                   at_uri: str, overall_risk: str, flagged: bool) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO published
           (published_at, source_id, observed_at, at_uri, overall_risk, flagged)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), source_id,
         observed_at, at_uri, overall_risk, int(flagged)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ATProto session (shared with node publisher — could extract to a lib later)
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
        resp = self.client.post(
            "/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {self.access_jwt}"},
            json={"repo": self.did, "collection": collection, "record": record},
        )
        resp.raise_for_status()
        return resp.json().get("uri", "")

    def create_post(self, text: str, tags: list[str] = None) -> str:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facets = []
        full_text = text
        if tags:
            tag_str = " " + " ".join(f"#{t}" for t in tags)
            full_text = text + tag_str
            full_bytes = full_text.encode("utf-8")
            for tag in tags:
                needle = f"#{tag}".encode("utf-8")
                idx = full_bytes.find(needle)
                if idx == -1:
                    continue
                facets.append({
                    "index": {"byteStart": idx, "byteEnd": idx + len(needle)},
                    "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
                })

        record = {"$type": "app.bsky.feed.post", "text": full_text, "createdAt": now}
        if facets:
            record["facets"] = facets
        return self.create_record("app.bsky.feed.post", record)


# ---------------------------------------------------------------------------
# Build synthesis lexicon record and post
# ---------------------------------------------------------------------------

def build_synthesis_record(row: dict, observed_at: str, synth_did: str) -> dict:
    return {
        "$type": LEXICON,
        "observedAt": observed_at,
        "nodeId": SYNTH_ID,
        "observationType": f"{LEXICON}#synthesis",
        "summary": row.get("summary", ""),
        "flagged": bool(row.get("flagged", False)),
        "flagReason": row.get("flag_reason", ""),
        "agentModel": "claude-sonnet-4-6",
        "synthesis": {
            "fireRisk":        row.get("fire_risk", "none"),
            "floodRisk":       row.get("flood_risk", "none"),
            "airQualityRisk":  row.get("air_quality_risk", "none"),
            "overallRisk":     row.get("overall_risk", "none"),
            "synthesisDid":    synth_did,
            "domainsObserved": ["watershed", "weather", "aqi"],
        },
    }


def build_post(row: dict) -> tuple[str, list[str]]:
    overall = row.get("overall_risk", "none")
    flagged = bool(row.get("flagged", False))
    summary = row.get("summary", "")

    risk_emoji = {"none": "🟢", "low": "🟢", "moderate": "🟡",
                  "high": "🟠", "extreme": "🔴"}.get(overall, "⚪")
    header = f"{risk_emoji} Napa Valley Advisory — {overall.upper()} risk"

    tags = ["NapaValley", "NapaFire", "NapaWeather"]
    if row.get("fire_risk") in ("high", "extreme"):
        tags.append("FireWeather")
    if row.get("flood_risk") in ("high", "extreme"):
        tags.append("FloodWatch")
    if flagged:
        tags.append("Flagged")

    tag_str = " " + " ".join(f"#{t}" for t in tags)
    overhead = len(header) + 2 + len(tag_str)
    max_summary = 300 - overhead
    if len(summary) > max_summary:
        summary = summary[:max_summary - 1] + "…"

    return f"{header}\n\n{summary}", tags


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesis ATProto publisher")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--synthesis-db", type=Path, default=_DEFAULT_SYNTHESIS_DB,
                        help="Path to synthesis.db (default: agent/data/synthesis.db)")
    parser.add_argument("--publisher-db", type=Path, default=_DEFAULT_PUBLISHER_DB,
                        help="Path to synth_publisher.db (default: data/synth_publisher.db)")
    args = parser.parse_args()

    SYNTHESIS_DB = args.synthesis_db

    handle = os.environ.get("BSKY_SYNTH_HANDLE", "").strip()
    app_password = os.environ.get("BSKY_SYNTH_APP_PASSWORD", "").strip()

    if not args.dry_run and (not handle or not app_password):
        raise RuntimeError(
            "BSKY_SYNTH_HANDLE and BSKY_SYNTH_APP_PASSWORD must be set. "
            "Create a Bluesky account for the synthesis identity and generate an App Password. "
            "This is a separate account from the node (BSKY_HANDLE)."
        )

    if not SYNTHESIS_DB.exists():
        log.warning("Synthesis DB not found at %s — nothing to publish", SYNTHESIS_DB)
        return

    pub_conn = init_publisher_db(args.publisher_db)

    # Read unpublished synthesis observations
    conn = sqlite3.connect(SYNTHESIS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid AS source_id, * FROM synthesis_observations ORDER BY rowid DESC LIMIT 1"
    ).fetchall()
    conn.close()

    unpublished = [dict(r) for r in rows if not already_published(pub_conn, r["source_id"])]
    if not unpublished:
        log.info("Nothing new to publish")
        return

    log.info("=== Synthesis Publisher starting ===")
    log.info("Account: %s  |  Dry run: %s  |  Unpublished: %d",
             handle or "(dry-run)", args.dry_run, len(unpublished))

    session = None
    if not args.dry_run:
        session = BlueskySession(handle, app_password)
        session.login()

    for row in reversed(unpublished):  # oldest first
        source_id = row["source_id"]
        observed_at = (row.get("observed_at") or "").replace("+00:00", "Z")
        if observed_at and not observed_at.endswith("Z"):
            observed_at += "Z"

        post_text, tags = build_post(row)
        overall_risk = row.get("overall_risk", "none")
        flagged = bool(row.get("flagged", False))

        if args.dry_run:
            log.info("[DRY RUN] Would publish synthesis observation %d:", source_id)
            log.info("  Risk: %s  |  Flagged: %s", overall_risk, flagged)
            log.info("  Post: %s", post_text[:120] + "...")
            mark_published(pub_conn, source_id, observed_at, "dry-run", overall_risk, flagged)
            continue

        try:
            record = build_synthesis_record(row, observed_at, session.did)
            record_uri = session.create_record(LEXICON, record)
            log.info("Published lexicon record: %s", record_uri)

            post_uri = session.create_post(post_text, tags)
            log.info("Published advisory post: %s", post_uri)

            mark_published(pub_conn, source_id, observed_at, record_uri, overall_risk, flagged)

        except httpx.HTTPStatusError as exc:
            log.error("Failed to publish observation %d: %s — %s",
                      source_id, exc, exc.response.text)
        except httpx.HTTPError as exc:
            log.error("Failed to publish observation %d: %s", source_id, exc)

    log.info("=== Synthesis Publisher complete ===")


if __name__ == "__main__":
    main()
