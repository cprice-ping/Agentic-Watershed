# OnlyPans

A collection site for copper cookware, built on ATProto. People keep the
identity they already have — `bsky.social` for most — and OnlyPans just stores
a different kind of record in it.

This is the ingest and image half. There is no web UI or OAuth writer yet; see
[Not built yet](#not-built-yet).

## How it fits together

```
  contributor's PDS                  ingest                    edge
  (bsky.social, or                   
   self-hosted)                      
  ┌──────────────┐                ┌──────────────┐         ┌──────────────┐
  │ pan record   │──── Jetstream ─→│ consumer.py  │         │  Worker      │
  │ + photo blob │   (filtered to  │              │         │              │
  └──────────────┘    our NSID)    │  SQLite ─────┼── read ─┤  /raw /img   │
         ▲                         │  index       │  model  │              │
         │                         │              │         └──────┬───────┘
         │                         │  blob        │                │
         └── getBlob ──────────────┼─ allowlist ──┼── KV ──────────→│
             (on cache miss)       └──────────────┘                 │
                                                              R2 ───┘
                                                            (durable cache)
```

Four moving parts:

| Path | What it is |
|---|---|
| `lexicon/` | The `net.cpricedomain.onlypans.pan` record schema |
| `ingest/consumer.py` | Jetstream consumer → SQLite index + blob allowlist |
| `worker/` | Cloudflare Worker: caching, resizing image proxy |
| `tools/publish_pan.py` | Test publisher, so the pipeline can run before there's a UI |

## Why an image proxy at all

Custom-lexicon blobs get none of the CDN treatment `cdn.bsky.app` gives
`app.bsky.*` records. Left alone, every photo in a gallery is a full-size
original fetched live from whichever PDS its owner happens to use — no
thumbnails, no format negotiation, no edge caching. For a photo-first site
that's the difference between usable and not, and it's invisible until you
build the gallery.

The Worker fills that gap. The design leans on one property: **blob CIDs are
content hashes**, so a URL can never mean different bytes than it did before.
Everything is `immutable`, the ETag is just the CID, and cache invalidation —
normally the hard half of running a CDN — is not a problem that exists here.

## The allowlist is load-bearing

A Worker that will fetch any blob from any DID's PDS on request is an open
proxy: anyone can point it at arbitrary repos and burn your bandwidth and R2
storage. So the Worker serves a `{did}/{cid}` pair only if it's present in KV,
and `consumer.py` writes those keys only for blobs referenced by pan records it
has actually indexed.

The same mechanism handles deletes. Once photos are mirrored into R2 this is
hosting user content, not just proxying it — so when a record is deleted, or
edited to swap a photo out, the consumer revokes the orphaned blobs from KV and
the Worker stops serving them. Revocation is refcounted: the same CID embedded
in two records survives one of them being deleted.

## Setup

### 1. Ingest

```bash
cd OnlyPans/ingest
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# index only, no Cloudflare — good first run
.venv/bin/python consumer.py --timeout 60 --no-kv
```

### 2. Cloudflare bindings

```bash
cd OnlyPans/worker
npm install
npx wrangler kv namespace create onlypans-blobs   # put the id in wrangler.toml
npx wrangler r2 bucket create onlypans-photos
npx wrangler deploy
```

Then give the consumer write access to KV:

```bash
export CF_ACCOUNT_ID=...
export CF_KV_NAMESPACE_ID=...
export CF_API_TOKEN=...        # needs Workers KV Storage:Edit
```

**Image Resizing** must be enabled on the zone for `/img/` to actually resize.
Without it those routes still work — they just return the original bytes, so
the site degrades to slow rather than broken.

### 3. Publish something

```bash
export BSKY_HANDLE=you.bsky.social
export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx

cd OnlyPans/tools
python3 publish_pan.py --title "3.5mm Dehillerin rondeau" \
    --photo ~/pans/rondeau.jpg --alt "Top view, freshly retinned" \
    --maker "E. Dehillerin" --form rondeau --lining tin \
    --diameter-mm 280 --thickness-mm 3.5 --condition retinned
```

The app password here is fine because you're publishing to your own account
from your own machine. It is **not** how contributors would ever log in — see
below.

### 4. Watch it land

```bash
cd OnlyPans/ingest
.venv/bin/python consumer.py --timeout 60
```

Then `https://img.onlypans.app/img/{did}:{cid}/400`.

## Running it for real

`consumer.py` is a long-lived process, unlike the cron-driven agents elsewhere
in this repo — Jetstream is a live stream, and its replay buffer is hours, not
days. Run it under systemd (see `Synthesis/watershed-subscriber.service` for
the pattern in use here) rather than on a timer.

If a contributor is on a self-hosted PDS that no relay crawls, Jetstream will
never see them. `--backfill <did>` reads that repo directly via `listRecords`.

## Not built yet

- **OAuth writer.** The real blocker for anyone but you. ATProto OAuth means
  hosting a client-metadata JSON document and handling DPoP. Until it exists,
  nobody else can post.
- **The app view's read side.** SQLite is the index; there's no HTTP API or web
  UI over it yet.
- **Moderation.** Fine to ignore at five users, not fine at five hundred.
- **Bluesky cross-posting.** A pan record renders nowhere on `bsky.app`. If
  posts should be visible where people already are, that means also writing an
  `app.bsky.feed.post` — the same dual-write the Synthesis agent does.

## Notes

The NSID sits under `net.cpricedomain` to match the existing watershed lexicon.
If OnlyPans gets its own domain it should move to `app.onlypans.pan` or
similar — NSID authority is tied to domain ownership, and that matters more now
that lexicon resolution over DNS exists. Moving it later means every existing
record keeps the old NSID, so it's cheaper to decide before there's real data.
