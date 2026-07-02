# Observation Viewer

A static page that shows both sides of an observation: the (necessarily
truncated) Bluesky advisory post, and the full underlying record — complete
reasoning, risk breakdown, and the domain agent observations that fed it.

No backend. `com.atproto.repo.getRecord` and `listRecords` are public,
unauthenticated reads on any PDS, so the page fetches records client-side
directly from wherever they live (Synthesis's PDS for the advisory,
the node's self-hosted PDS for domain records) — same pattern as
`curl`-ing them by hand, just rendered.

## How it's linked

`Synthesis/publisher.py` appends a link to every advisory post pointing at:

```
https://viewer.watershed-agent.dev/?uri=at://<synthesis-did>/net.cpricedomain.temp.monitor.observation/<rkey>
```

The viewer resolves that `at://` URI's DID via the public PLC directory
(`plc.directory`) to find its PDS, fetches the full synthesis record, then
separately fetches domain records from `publishers.json`'s trusted node
DIDs and filters to the same lookback window (24h) Synthesis itself used
when it ran — there's no explicit link from a synthesis record to the
specific domain records it read, so this is an approximation by time window,
not an exact reference.

## `publishers.json`

Mirrors `Synthesis/publishers.json`. This page has no server, so it can't
read the repo's copy directly — **keep this file in sync by hand** when a
node's DID changes or a new node is added. (Small enough scale that this is
fine; if the node count grows meaningfully, worth fetching this from a
public URL instead of duplicating it.)

## Deploying (Cloudflare Workers, static assets)

Cloudflare's dashboard now funnels Git-connected deploys through **Workers &
Pages → Create → Import a repository**, which defaults to Wrangler-based
deployment rather than the older "Pages: build output directory" flow. This
repo's root `wrangler.toml` configures a static-assets-only Worker (no
code) pointing at `Viewer/` — that's what makes the default deploy command
(`npx wrangler deploy`) work here.

1. **Workers & Pages → Create → Import a repository**, pick this repo.
   Leave **Build command** blank, leave **Deploy command** as
   `npx wrangler deploy` — it picks up `wrangler.toml` automatically.
2. Click **Deploy**. First deploy gives you a `*.workers.dev` URL — check
   that the prompt view loads before wiring up the real domain.
3. **Custom domain**: in the deployed project → **Settings → Domains &
   Routes → Add → Custom domain** → `viewer.watershed-agent.dev`. Since
   `watershed-agent.dev` is already on Cloudflare (registered via Cloudflare
   Registrar — see `ATProto/pds/README.md`), this just adds a DNS record in
   the same dashboard — no separate tunnel needed, unlike the PDS which
   needs `cloudflared` to reach the Pi.
4. Every push to `main` that touches `Viewer/` or `wrangler.toml`
   redeploys automatically.

## Local testing

```bash
cd Viewer && python3 -m http.server 8080
# open http://localhost:8080/?uri=at://did:plc:.../net.cpricedomain.temp.monitor.observation/...
```

## Known limitations

- Domain-record matching is time-window based, not an exact reference —
  see "How it's linked" above.
- If a PDS doesn't serve CORS headers on its XRPC endpoints, the fetch
  will fail silently in-browser. The official `bluesky-social/pds` and
  `bsky.social` both do; worth checking if a future node runs something
  else.
- `Viewer/publishers.json` needs manual updates — see above.
