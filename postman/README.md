# Postman collection — ATProto API calls

`Agentic-Watershed-ATProto.postman_collection.json` covers every ATProto XRPC
call this project actually makes, grouped by which file makes it — a way to
see the raw HTTP shape underneath `publisher.py`/`subscriber.py`/the Viewer
without reading Python or JS.

## Import

Postman → **Import** → select the `.json` file. Six folders, in the order
you'd naturally exercise them:

1. **Node PDS — Identity & Auth** — health check, account creation, login
2. **Node PDS — Writing records** — the three domain observation shapes
   `ATProto/publisher.py` actually publishes
3. **Synthesis PDS — Identity & Auth** — same login flow, different PDS
   (`bsky.social`, not self-hosted — see `CONTEXT.md` for why)
4. **Synthesis PDS — Writing** — the synthesis lexicon record (with the
   full `reasoning` field) and the human-facing Bluesky advisory post
5. **Reading records** — `listRecords`/`getRecord`, all unauthenticated —
   what `subscriber.py` and the Viewer actually do
6. **DID Resolution** — the separate `plc.directory` lookups that find each
   DID's actual PDS

## Before you run anything

Set `node_app_password` and `synth_app_password` in a **Postman
Environment**, not in the collection itself — the collection file is meant
to be committed to git (no real secrets in it); environments aren't.

Run **Create Session** in folders 1 and 3 first — both have test scripts
that auto-populate `node_access_jwt`/`synth_access_jwt` (and the DIDs, if
you cleared them) as collection variables, so every authenticated request
after that just works without manually copying tokens around.

## Why two PDSs

Folders 1–2 hit the node's own self-hosted PDS
(`napa-node-01.watershed-agent.dev`); folders 3–4 hit `bsky.social`. That's
not a mistake — the node's domain agents publish to their own identity with
no public-facing purpose, while Synthesis is the one component that also
needs to be readable in a normal Bluesky client, so it keeps a real Bluesky
account. See `CONTEXT.md`'s "Why a self-hosted PDS instead of publishing to
bsky.social?" for the full reasoning.

## What's real vs. illustrative

The request bodies use real field shapes and real DIDs (both are public
knowledge — DIDs and repo contents are unauthenticated public reads by
design, see folder 5), but example values like timestamps and summaries are
representative, not live data. Running **Create Record** requests against
the real PDSs will actually publish records — treat them like any other
write call, not a read-only exploration.
