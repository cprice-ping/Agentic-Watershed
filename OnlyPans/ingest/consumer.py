"""
OnlyPans Jetstream Consumer
---------------------------
Subscribes to Jetstream filtered to a single collection — the OnlyPans pan
lexicon — and maintains two things:

  1. A local SQLite index of every pan record on the network (the app view's
     read model).
  2. A {did}/{cid} blob allowlist, mirrored into Cloudflare KV, which the
     image Worker checks before it will proxy anything. Without this the
     Worker is an open proxy against every PDS in the network.

Jetstream rather than the raw firehose: filtering happens server-side via
wantedCollections, so instead of decoding every CBOR commit on the network
we receive only pan records — a handful a day. Same reason the raw firehose
path in Synthesis/subscriber.py ended up being the legacy option.

Blob CIDs are not in the commit envelope. Jetstream renders the record as
JSON, so blob refs arrive as {"$type": "blob", "ref": {"$link": "bafkre..."}}
inside photos[] and have to be walked out of the record body.

Coverage caveat, same as Synthesis: Jetstream sees what the relay crawls. A
self-hosted PDS that no relay has been told about will not appear. Use
--backfill for those, which reads the repo directly.

Usage:
  python consumer.py                          # stream forever from last cursor
  python consumer.py --timeout 120            # stream for 2 minutes, then exit
  python consumer.py --cursor 0               # replay from the start of Jetstream's window
  python consumer.py --backfill did:plc:abc…  # pull one repo directly via listRecords
  python consumer.py --sync-kv                # re-push the whole allowlist to KV, then exit
  python consumer.py --no-kv                  # index only, skip Cloudflare entirely

Environment (KV sync is skipped with a warning if these are unset):
  CF_ACCOUNT_ID          Cloudflare account ID
  CF_KV_NAMESPACE_ID     KV namespace ID for the blob allowlist
  CF_API_TOKEN           API token with Workers KV Storage:Edit
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEXICON = "net.cpricedomain.onlypans.pan"

DB_PATH = Path(__file__).parent / "data" / "onlypans.db"

# Public Jetstream instances. Rotated through on reconnect so a single
# instance going down doesn't stall ingest.
JETSTREAM_HOSTS = [
    "jetstream1.us-east.bsky.network",
    "jetstream2.us-east.bsky.network",
    "jetstream1.us-west.bsky.network",
    "jetstream2.us-west.bsky.network",
]

PLC_DIRECTORY = "https://plc.directory"

# Jetstream's replay buffer is finite (hours, not days). On reconnect we rewind
# the cursor slightly rather than resuming at the exact last event, because
# duplicate deliveries are free — every write here is idempotent — while a gap
# silently loses a record forever.
CURSOR_REWIND_US = 5_000_000  # 5 seconds

CF_ACCOUNT_ID      = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN       = os.environ.get("CF_API_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("onlypans.consumer")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pans (
            at_uri        TEXT PRIMARY KEY,
            did           TEXT NOT NULL,
            rkey          TEXT NOT NULL,
            record_cid    TEXT,
            indexed_at    TEXT NOT NULL,
            created_at    TEXT,
            title         TEXT,
            notes         TEXT,
            maker         TEXT,
            material      TEXT,
            lining        TEXT,
            form          TEXT,
            diameter_mm   INTEGER,
            height_mm     INTEGER,
            thickness_mm  REAL,
            weight_g      INTEGER,
            handle_material TEXT,
            era           TEXT,
            condition     TEXT,
            restoration   TEXT,
            provenance    TEXT,
            acquired_at   TEXT,
            raw_record    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_pans_did     ON pans (did, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pans_created ON pans (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pans_maker   ON pans (maker);
        CREATE INDEX IF NOT EXISTS idx_pans_form    ON pans (form);

        -- Blob allowlist. One row per (did, cid) the image Worker may serve.
        -- at_uri is carried so a record delete can revoke exactly the blobs
        -- that record introduced and nothing else.
        CREATE TABLE IF NOT EXISTS blobs (
            did        TEXT NOT NULL,
            cid        TEXT NOT NULL,
            at_uri     TEXT NOT NULL,
            mime_type  TEXT,
            size       INTEGER,
            synced_at  TEXT,
            PRIMARY KEY (did, cid, at_uri)
        );

        CREATE INDEX IF NOT EXISTS idx_blobs_pair   ON blobs (did, cid);
        CREATE INDEX IF NOT EXISTS idx_blobs_unsync ON blobs (synced_at) WHERE synced_at IS NULL;

        -- Blobs whose last referencing record was deleted. The KV sync drains
        -- this into DELETE calls, then clears it. A separate table rather than
        -- an immediate delete so that a KV outage doesn't lose the revocation.
        CREATE TABLE IF NOT EXISTS blob_revocations (
            did       TEXT NOT NULL,
            cid       TEXT NOT NULL,
            queued_at TEXT NOT NULL,
            PRIMARY KEY (did, cid)
        );

        CREATE TABLE IF NOT EXISTS ingest_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM ingest_state WHERE key = 'cursor'"
    ).fetchone()
    return int(row["value"]) if row else None


def set_cursor(conn: sqlite3.Connection, cursor: int) -> None:
    conn.execute(
        "INSERT INTO ingest_state (key, value) VALUES ('cursor', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(cursor),),
    )


# ---------------------------------------------------------------------------
# Record handling
# ---------------------------------------------------------------------------

def extract_blob_refs(record: dict) -> list[dict]:
    """Walk a record for blob references.

    Recursive rather than reading record["photos"] directly so that adding a
    blob field to the lexicon later — a maker's-mark closeup, a receipt scan —
    doesn't silently fail to allowlist its images.
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            # JSON-rendered blob: {"$type": "blob", "ref": {"$link": "bafkre…"}}
            ref = node.get("ref")
            if node.get("$type") == "blob" and isinstance(ref, dict) and ref.get("$link"):
                found.append({
                    "cid": ref["$link"],
                    "mimeType": node.get("mimeType"),
                    "size": node.get("size"),
                })
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(record)
    return found


def store_pan(conn: sqlite3.Connection, did: str, rkey: str,
              record_cid: str | None, record: dict) -> None:
    """Upsert a pan record and allowlist its blobs."""
    at_uri = f"at://{did}/{LEXICON}/{rkey}"
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO pans (
            at_uri, did, rkey, record_cid, indexed_at, created_at, title, notes,
            maker, material, lining, form, diameter_mm, height_mm, thickness_mm,
            weight_g, handle_material, era, condition, restoration, provenance,
            acquired_at, raw_record
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(at_uri) DO UPDATE SET
            record_cid      = excluded.record_cid,
            indexed_at      = excluded.indexed_at,
            created_at      = excluded.created_at,
            title           = excluded.title,
            notes           = excluded.notes,
            maker           = excluded.maker,
            material        = excluded.material,
            lining          = excluded.lining,
            form            = excluded.form,
            diameter_mm     = excluded.diameter_mm,
            height_mm       = excluded.height_mm,
            thickness_mm    = excluded.thickness_mm,
            weight_g        = excluded.weight_g,
            handle_material = excluded.handle_material,
            era             = excluded.era,
            condition       = excluded.condition,
            restoration     = excluded.restoration,
            provenance      = excluded.provenance,
            acquired_at     = excluded.acquired_at,
            raw_record      = excluded.raw_record
        """,
        (
            at_uri, did, rkey, record_cid, now,
            record.get("createdAt"),
            record.get("title"),
            record.get("notes"),
            record.get("maker"),
            record.get("material"),
            record.get("lining"),
            record.get("form"),
            record.get("diameterMm"),
            record.get("heightMm"),
            record.get("thicknessMm"),
            record.get("weightG"),
            record.get("handleMaterial"),
            record.get("era"),
            record.get("condition"),
            record.get("restoration"),
            record.get("provenance"),
            record.get("acquiredAt"),
            json.dumps(record, separators=(",", ":")),
        ),
    )

    # Allowlist current blobs, and revoke any this record used to reference —
    # an edit that swaps a photo should stop the old one being served.
    blobs = extract_blob_refs(record)
    current_cids = {b["cid"] for b in blobs}

    stale = [
        r["cid"] for r in conn.execute(
            "SELECT cid FROM blobs WHERE at_uri = ?", (at_uri,)
        ).fetchall()
        if r["cid"] not in current_cids
    ]
    for cid in stale:
        conn.execute("DELETE FROM blobs WHERE at_uri = ? AND cid = ?", (at_uri, cid))
        _revoke_if_orphaned(conn, did, cid)

    for blob in blobs:
        conn.execute(
            """
            INSERT INTO blobs (did, cid, at_uri, mime_type, size, synced_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(did, cid, at_uri) DO UPDATE SET
                mime_type = excluded.mime_type,
                size      = excluded.size
            """,
            (did, blob["cid"], at_uri, blob.get("mimeType"), blob.get("size")),
        )
        # A re-added blob may be sitting in the revocation queue — cancel that.
        conn.execute(
            "DELETE FROM blob_revocations WHERE did = ? AND cid = ?", (did, blob["cid"])
        )

    log.info("indexed %s (%s) — %d photo(s)", at_uri, record.get("title", "untitled"), len(blobs))


def _revoke_if_orphaned(conn: sqlite3.Connection, did: str, cid: str) -> None:
    """Queue a blob for KV deletion once no remaining record references it.

    The same blob CID can legitimately appear in more than one record — the
    same upload embedded twice — so revocation is refcounted, not immediate.
    """
    still_used = conn.execute(
        "SELECT 1 FROM blobs WHERE did = ? AND cid = ? LIMIT 1", (did, cid)
    ).fetchone()
    if still_used:
        return
    conn.execute(
        "INSERT OR IGNORE INTO blob_revocations (did, cid, queued_at) VALUES (?, ?, ?)",
        (did, cid, datetime.now(timezone.utc).isoformat()),
    )


def delete_pan(conn: sqlite3.Connection, did: str, rkey: str) -> None:
    """Remove a record and revoke the blobs it alone was keeping alive."""
    at_uri = f"at://{did}/{LEXICON}/{rkey}"

    cids = [r["cid"] for r in conn.execute(
        "SELECT cid FROM blobs WHERE at_uri = ?", (at_uri,)
    ).fetchall()]

    conn.execute("DELETE FROM blobs WHERE at_uri = ?", (at_uri,))
    conn.execute("DELETE FROM pans  WHERE at_uri = ?", (at_uri,))

    for cid in cids:
        _revoke_if_orphaned(conn, did, cid)

    log.info("deleted %s — revoked %d blob(s)", at_uri, len(cids))


def handle_event(conn: sqlite3.Connection, evt: dict) -> None:
    if evt.get("kind") != "commit":
        return

    commit = evt.get("commit") or {}
    if commit.get("collection") != LEXICON:
        return  # server-side filter should prevent this; cheap to be sure

    did  = evt["did"]
    rkey = commit.get("rkey")
    op   = commit.get("operation")

    if op in ("create", "update"):
        record = commit.get("record")
        if not isinstance(record, dict):
            log.warning("commit %s/%s has no record body, skipping", did, rkey)
            return
        store_pan(conn, did, rkey, commit.get("cid"), record)
    elif op == "delete":
        delete_pan(conn, did, rkey)
    else:
        log.debug("ignoring operation %r", op)


# ---------------------------------------------------------------------------
# Cloudflare KV sync — the image Worker's allowlist
# ---------------------------------------------------------------------------

def kv_configured() -> bool:
    return all([CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN])


def _kv_url(suffix: str) -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/{suffix}"
    )


def sync_kv(conn: sqlite3.Connection, resync_all: bool = False) -> None:
    """Push pending allowlist additions and revocations to Cloudflare KV.

    Additions are written before revocations. If the process dies between the
    two, the failure mode is a blob that stays servable slightly too long,
    rather than a live photo 404ing.
    """
    if not kv_configured():
        log.warning(
            "Cloudflare KV not configured (CF_ACCOUNT_ID / CF_KV_NAMESPACE_ID / "
            "CF_API_TOKEN) — index is up to date but the Worker allowlist is not"
        )
        return

    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}

    query = (
        "SELECT DISTINCT did, cid, mime_type FROM blobs"
        if resync_all else
        "SELECT DISTINCT did, cid, mime_type FROM blobs WHERE synced_at IS NULL"
    )
    additions = conn.execute(query).fetchall()

    with httpx.Client(timeout=30, headers=headers) as client:
        for batch in _chunks(additions, 100):  # Cloudflare bulk limit is 10k; 100 keeps payloads small
            payload = [
                {
                    "key": f"{r['did']}/{r['cid']}",
                    # The Worker reads mime type from here so it can set a
                    # correct Content-Type without trusting the upstream PDS
                    # response or sniffing bytes.
                    "value": json.dumps({"mimeType": r["mime_type"] or "application/octet-stream"}),
                }
                for r in batch
            ]
            resp = client.put(_kv_url("bulk"), json=payload)
            resp.raise_for_status()

        if additions:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("UPDATE blobs SET synced_at = ? WHERE synced_at IS NULL", (now,))
            conn.commit()
            log.info("KV: allowlisted %d blob(s)", len(additions))

        revocations = conn.execute("SELECT did, cid FROM blob_revocations").fetchall()
        for batch in _chunks(revocations, 100):
            keys = [f"{r['did']}/{r['cid']}" for r in batch]
            resp = client.request("DELETE", _kv_url("bulk"), json=keys)
            resp.raise_for_status()

        if revocations:
            conn.execute("DELETE FROM blob_revocations")
            conn.commit()
            log.info("KV: revoked %d blob(s)", len(revocations))


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------------------
# Backfill — direct repo read, for PDSs the relay doesn't crawl
# ---------------------------------------------------------------------------

def resolve_pds(did: str) -> str:
    """Resolve a DID to its PDS endpoint.

    Same approach as Synthesis/subscriber.py: resolve per-DID rather than
    assuming a shared host, so a self-hosted PDS and bsky.social can both be
    in the collection at once.
    """
    if not did.startswith("did:plc:"):
        raise ValueError(f"Unsupported DID method: {did}")
    resp = httpx.get(f"{PLC_DIRECTORY}/{did}", timeout=15)
    resp.raise_for_status()
    svc = next(
        (s for s in resp.json().get("service", []) if s.get("id") == "#atproto_pds"),
        None,
    )
    if not svc:
        raise ValueError(f"No PDS service endpoint in DID document for {did}")
    return svc["serviceEndpoint"]


def backfill(conn: sqlite3.Connection, did: str) -> int:
    """Read every pan record straight out of one repo via listRecords."""
    pds = resolve_pds(did)
    log.info("backfilling %s from %s", did, pds)

    count, cursor = 0, None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"repo": did, "collection": LEXICON, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = client.get(f"{pds}/xrpc/com.atproto.repo.listRecords", params=params)
            resp.raise_for_status()
            body = resp.json()

            for item in body.get("records", []):
                rkey = item["uri"].rsplit("/", 1)[-1]
                store_pan(conn, did, rkey, item.get("cid"), item["value"])
                count += 1

            conn.commit()
            cursor = body.get("cursor")
            if not cursor:
                break

    log.info("backfilled %d record(s) from %s", count, did)
    return count


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------

async def stream(conn: sqlite3.Connection, start_cursor: int | None,
                 timeout: float | None, sync_kv_enabled: bool) -> None:
    started = time.monotonic()
    cursor = start_cursor
    host_idx = 0
    backoff = 1

    while True:
        if timeout and (time.monotonic() - started) >= timeout:
            log.info("timeout reached, stopping")
            return

        host = JETSTREAM_HOSTS[host_idx % len(JETSTREAM_HOSTS)]
        url = f"wss://{host}/subscribe?wantedCollections={LEXICON}"
        if cursor is not None:
            url += f"&cursor={max(0, cursor - CURSOR_REWIND_US)}"

        try:
            log.info("connecting to %s (cursor=%s)", host, cursor)
            async with websockets.connect(url, max_size=5_000_000) as ws:
                backoff = 1
                pending = 0

                while True:
                    remaining = None
                    if timeout:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            conn.commit()
                            if sync_kv_enabled:
                                sync_kv(conn)
                            log.info("timeout reached, stopping")
                            return

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        continue

                    evt = json.loads(raw)
                    if "time_us" in evt:
                        cursor = evt["time_us"]

                    before = conn.total_changes
                    handle_event(conn, evt)
                    if conn.total_changes != before:
                        pending += 1

                    set_cursor(conn, cursor)

                    # Commit and push to KV as soon as anything real lands.
                    # Volume here is a few records a day, so there is no
                    # batching win to trade against the risk of losing work.
                    if pending:
                        conn.commit()
                        if sync_kv_enabled:
                            try:
                                sync_kv(conn)
                            except httpx.HTTPError as exc:
                                log.error("KV sync failed (will retry next event): %s", exc)
                        pending = 0
                    else:
                        conn.commit()

        except (ConnectionClosed, OSError) as exc:
            conn.commit()
            log.warning("connection lost (%s) — reconnecting in %ds", exc, backoff)
            host_idx += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="OnlyPans Jetstream consumer")
    ap.add_argument("--cursor", type=int,
                    help="Start from this Jetstream cursor (microseconds). 0 replays the full buffer.")
    ap.add_argument("--timeout", type=float,
                    help="Stop after this many seconds. Useful for testing.")
    ap.add_argument("--backfill", metavar="DID",
                    help="Read one repo directly via listRecords instead of streaming.")
    ap.add_argument("--sync-kv", action="store_true",
                    help="Re-push the entire allowlist to Cloudflare KV, then exit.")
    ap.add_argument("--no-kv", action="store_true",
                    help="Index only — never talk to Cloudflare.")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="SQLite path.")
    args = ap.parse_args()

    conn = init_db(args.db)
    sync_kv_enabled = not args.no_kv

    if args.sync_kv:
        sync_kv(conn, resync_all=True)
        return

    if args.backfill:
        backfill(conn, args.backfill)
        if sync_kv_enabled:
            sync_kv(conn)
        return

    cursor = args.cursor if args.cursor is not None else get_cursor(conn)

    try:
        asyncio.run(stream(conn, cursor, args.timeout, sync_kv_enabled))
    except KeyboardInterrupt:
        conn.commit()
        log.info("interrupted, cursor saved")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
