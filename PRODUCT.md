# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: readers of the Synthesis advisory on Bluesky (`napasynth01.bsky.social`) who click through the `?uri=` link appended to every advisory post. They arrive holding a truncated summary with one question — can I trust this? — and the Viewer's job is to answer it with the full record.

Other audiences exist in practice (the operator checking what the agents published, technically curious visitors evaluating the pattern) but are not confirmed design targets.

## Product Purpose

The Observation Viewer shows both sides of an agent observation: the necessarily truncated Bluesky advisory post, and the full underlying ATProto record — complete reasoning, risk breakdown, and the domain-agent observations that fed it. Success means a reader can verify or challenge the advisory from the receipts, without having to trust anyone's summary.

## Positioning

The viewer renders the record itself, from the source. No backend, no database, no cached copy: the page resolves the record's DID via the public PLC directory, fetches it from whichever PDS hosts it, and displays exactly what the federation published. The human audience reads the same records the Synthesis agent consumed — the advisory and its evidence, read directly off the wire.

## Operating Context

- Arrival is a link with `?uri=at://...` appended by `Synthesis/publisher.py` to every Bluesky advisory post; bare visits auto-load the latest synthesis record, falling back to a manual URI prompt.
- Deployed as a static-assets Cloudflare Worker at `viewer.watershed-agent.dev` (root `wrangler.toml`); pushes to `main` touching `Viewer/` redeploy automatically. Local test: `cd Viewer && python3 -m http.server 8080`.
- The whole surface is one self-contained `Viewer/index.html` (~450 lines, no framework, no build step) plus `Viewer/publishers.json`.
- Records live on two PDSes: the Synthesis identity (`did:plc:clcw2dxrd6qma45gy3oozjwa`) and the node's self-hosted PDS (`napa-node-01.watershed-agent.dev`, `did:plc:ggztd5hjk3cnkhgzdk4rmqan`, behind a Cloudflare Tunnel on a Raspberry Pi).
- All fetches happen in the visitor's browser and are public and unauthenticated — CORS support on the serving PDS is a hard dependency (the official `bluesky-social/pds` provides it).

## Capabilities and Constraints

- Lexicon: `net.cpricedomain.temp.monitor.observation`; `observationType` distinguishes `#synthesis` from domain records (`#watershed`, `#weather`, `#aqi`, `#fire`). Risk vocabulary is `none / low / moderate / high / extreme`. Records carry `observedAt`, `summary`, `flagged`/`flagReason`, per-domain numeric fields, and (on synthesis records) full `reasoning`.
- Domain-record matching is a time-window approximation: the viewer filters to the same 24h lookback window Synthesis used, because synthesis records don't reference the specific domain records they drew from. A stated limitation of the design, not a bug.
- `Viewer/publishers.json` mirrors `Synthesis/publishers.json` and is updated by hand (one node today; fine at this scale).
- ATProto is DAG-CBOR: all numerics in records are strings, never floats.
- Explicitly undecided: the Viewer README promises "both sides" — the Bluesky advisory post *and* the record — but the current page renders only the record side and never fetches the Bluesky post. Whether to fetch and render the post is an open product decision, not a settled requirement.
- Error and empty-state handling exists but the surface has never had a hardening pass.

## Brand Commitments

- Name: "Agentic Watershed"; viewer subdomain `viewer.watershed-agent.dev` under `watershed-agent.dev`.
- The pipeline's identities are part of the product's meaning, not just config: `napasynth01.bsky.social` (the human-facing advisory voice) and `napa-node-01.watershed-agent.dev` (the edge node). No logo or wordmark exists.

## Evidence on Hand

- Live records on both PDSes — the viewer's own data source, fetchable by hand via `com.atproto.repo.getRecord` / `listRecords`.
- `Viewer/README.md` documents intent, linkage, deployment, and known limitations; repo `README.md` and `CONTEXT.md` document the architecture, deployment state, and project lineage.
- No testimonials, screenshots, or marketing assets exist. None may be fabricated.

## Product Principles

1. **Receipts over takeaways.** The job is verification. Show the full reasoning and the underlying records; never summarize away the evidence a reader needs to challenge the advisory.
2. **The record is the interface.** Render what the federation actually published, fetched from its source of truth. No editorial layer between the record and the reader.
3. **Zero-backend by construction.** Public, unauthenticated reads only. Anything that would require a server is out of scope for this surface.
4. **Provenance is content.** DIDs, `at://` URIs, timestamps, and the trusted-publisher registry are part of what a reader comes to see, not metadata to hide.
5. **One advisory, done completely.** A focused single-record surface. No dashboard, history, or trends unless that product decision changes.
