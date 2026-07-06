# Project Context — Agentic Watershed

Living document. Update this as the project evolves so coding agents and
collaborators can pick up where things left off without needing the full
conversation history.

Last updated: 2026-07-02

---

## What this is

A distributed system of autonomous agents connected by identity and a federated protocol.
The domain is Napa Valley environmental data. That's the concrete surface — the actual
subject is the architecture:

- **Edge agents with workload identity** — each node runs on a Raspberry Pi, reasons
  locally with Claude Haiku, and publishes structured records to ATProto under its own
  DID. The DID is the agent's identity, not a login credential.
- **ATProto as message bus, not Bluesky as destination** — domain agents publish to
  their own self-hosted PDS (`napa-node-01.watershed-agent.dev`), reachable via a
  Cloudflare Tunnel — no dependency on `bsky.social` infrastructure for the node's
  identity or data. Only Synthesis touches the public Bluesky network, and only for
  the human-facing advisory. Records flow over a federated protocol using a custom
  lexicon (`net.cpricedomain.temp.monitor.observation`). Any agent that knows the
  lexicon and trusts the DID can participate, from anywhere.
- **DID-based trust boundary** — the Synthesis agent verifies publisher DIDs against
  a trusted registry (`publishers.json`) before acting on any record. Unrecognised
  nodes are rejected, not silently trusted.
- **Synthesis at the cloud layer** — a separate agent (Azure Container Apps Job)
  fetches from the node's PDS, reasons across domains with Claude Sonnet, resolves
  its own prediction ledger, and posts the human-facing advisory to Bluesky
  (`napasynth01.bsky.social`) — the only step in the pipeline a person ever sees.

The environmental monitoring domain is well-suited because it has real APIs, genuine
cross-domain reasoning, and seasonal patterns worth tracking over time. The architecture
pattern (edge agent → structured record → verified identity → synthesis) is the point.

---

## Lineage and kindred work

The seed was Ruthanna Emrys' novel **"A Half-Built Garden"** — a vision of
decentralised, federated systems operating at human (and non-human) scale without
a central authority, and of non-human participants treated as first-class actors in
a shared network. Watching what **AT Protocol** was actually building — portable
identity (`did:plc`), data bound to the identity rather than the platform, and
federation as a first principle — made it concrete: this is the closest existing
substrate for agents that own their identity and history independent of where they run.

Identity is a small but critical part of the bigger idea. The bigger idea is
autonomous agents as portable, verifiable, federated participants in a shared
information space — perceiving, reasoning, and publishing without central coordination
or a human in the loop. Identity is what makes the trust boundary possible, but it
serves the architecture, not the other way around.

Adjacent thinkers and projects working nearby ground:

- **Bluesky / ATProto** — user-owned identity and data, PDS portability. The substrate.
- **Spritely Institute** (Christine Lemmer-Webber, co-author of ActivityPub) —
  object capabilities and distributed identity at the protocol level; the strongest
  "no central authority" research thread.
- **DIF (Decentralised Identity Foundation)** — standards home for `did:web`, VCs,
  and the agent-identity problem. The specs this project builds against.
- **Ink & Switch** (Geoffrey Litt et al.) — agents as first-class collaborators in
  systems rather than tools; shares the "agent as peer" framing.
- **Ceramic, Transmute (Orie Steele)** — decentralised data and machine/non-human
  identity, adjacent but more blockchain-native than this project needs.

**The unoccupied space:** nobody is quite using ATProto as the message bus for
agent-to-agent communication with DID-based trust between autonomous nodes. Most
agent-identity work is either blockchain-native (heavy) or OAuth-native (human-first).
The combination here — ATProto portability + `did:web` simplicity + autonomous edge
agents — is relatively unexplored. That's the generative gap.

---

## Current deployment state

Running on a Raspberry Pi 5, Napa, California.
All stacks deployed under `/home/cprice/Agentic-Watershed/`.

### Collectors — all running via cron

| Stack | Frequency | Status |
|-------|-----------|--------|
| Watershed | every 15 min | ✅ Running, storing to `watershed.db` |
| Weather | every 30 min | ✅ Running, storing to `weather.db` |
| AQI | every 30 min | ✅ Running, storing to `aqi.db` |
| Fire | every 30 min | ✅ Added 2026-07-06, storing to `fire.db` |

### Domain agents — all running via cron

| Stack | Schedule | Status |
|-------|----------|--------|
| Watershed | 0,6,12,18h | ✅ Running, writing observations |
| Weather | 1,7,13,19h | ✅ Running, writing observations |
| AQI | 2,8,14,20h | ✅ Running, writing observations |
| Fire | 3,9,15,21h | ✅ Added 2026-07-06, writing observations |

### Fire domain — NASA FIRMS satellite hotspot detection

Fourth domain, added 2026-07-06. Motivation: Synthesis's own cross-domain
reasoning had repeatedly flagged "the upwind fire source has not been
identified or confirmed extinguished" as an open uncertainty across
multiple real runs — Weather covers fire *weather*, AQI covers the *smoke
signature*, but nothing looked for an actual fire. This closes that gap
directly: [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) satellite
hotspot detections (VIIRS near-real-time), free public API, requires a
free `MAP_KEY` (env var `FIRMS_API_KEY`).

Deliberately does **not** attempt to attach named-incident data (e.g.
official CAL FIRE incident names) to detected hotspots. That was considered
and explicitly deferred — FIRMS is a clean, well-documented, versioned
public API on par with USGS/NWS/AirNow already in this project; a
named-incident feed (CAL FIRE or NIFC) is not documented to the same
standard and would need a second, less reliable data source plus a real
spatial-matching layer (nearest named incident within some distance of a
hotspot). Worth doing as a fast-follow once FIRMS itself is proven in
production, not as part of the first pass.

Same architecture pattern as every other domain: `Fire/collector.py` (FIRMS
Area API → `hotspots` table, deduped on lat/lon/acq_date/acq_time/satellite,
haversine distance from Napa center precomputed and stored) →
`Fire/mcp_server.py` (MCP tools, port 8003) → `Fire/agent.py` (Haiku, forced
tool-use from the start — built after the tool-use fix, so it never had the
free-text JSON parsing bug the other four agents needed fixing). Wired into
`ATProto/publisher.py` (`build_fire_record()`, `_fetch_fire_numerics()` —
nearest hotspot's distance/confidence/FRP plus a 6h hotspot count, same
DAG-CBOR string-not-float handling as the other domains) and into
Synthesis's reasoning (`agent_atproto.py`'s domain filter and system prompt
both updated — see "FIRE DETECTION" section of the system prompt for the
specific guidance: an empty hotspot list means "nothing detected in the
monitored bounding box," not "no fire," since a fire could be upwind but
outside the bbox or not yet caught by a satellite pass).

Bounding box (`node_config.json`'s `"fire"` block, not hardcoded):
`-123.3,37.7,-121.8,39.0`, roughly Napa/Sonoma/Solano/Lake counties — sized
for regional smoke-transport awareness, not just fires within county lines.

### Self-hosted PDS — node identity, off Bluesky infrastructure

| Component | Status |
|-----------|--------|
| PDS (official `bluesky-social/pds`, Docker on the Pi) | ✅ Running, `napa-node-01.watershed-agent.dev` |
| Cloudflare Tunnel (`cloudflared`, systemd service) | ✅ Live — no inbound ports opened on the Pi's router |
| Domain: `watershed-agent.dev` | Registered + DNS-hosted directly via Cloudflare Registrar |
| Node DID | `did:plc:ggztd5hjk3cnkhgzdk4rmqan` (replaces the old `bsky.social`-issued one) |

Domain agents publish structured lexicon records to this PDS, not to Bluesky —
`ATProto/publisher.py` no longer sends an accompanying `app.bsky.feed.post`. First
confirmed end-to-end publish 2026-07-01. See `ATProto/pds/README.md` for the full
setup (DNS delegation didn't work at the registrar level — see "Architecture
decisions" below for why a dedicated domain was registered instead).

### Synthesis agent

| Schedule | Status |
|----------|--------|
| 6h, 18h UTC | ✅ Running in Azure Container Apps Job (`synthesis-agent`, `rg-agentic-watershed`, `westus2`) |

The Synthesis agent runs in Azure, not on the laptop. Pi (edge) → self-hosted PDS →
Azure (cloud) → Bluesky advisory. Redeployed 2026-07-02 with `publishers.json`
updated to the new DID — confirmed live: subscriber fetched 8 records from
`napa-node-01.watershed-agent.dev`, resolved 2 pending predictions against real
data, reasoned across domains, posted the advisory to `napasynth01.bsky.social`.

That first redeploy used a single global `ATPROTO_PDS_URL` for all fetches —
fine with one node, but silently wrong once a second node runs its own separate
PDS (it would only ever query the configured URL, missing the other node's
records with no error). `subscriber.py` now resolves each trusted publisher's
PDS individually via `plc.directory`, same pattern as `Viewer/index.html`'s
`resolvePds()` — see "DID resolution" below. `ATPROTO_PDS_URL` is no longer
read by the subscriber or set on the Azure job.

Accumulating domain observations — first meaningful cross-domain synthesis
expected after 2-3 days of data. Baseline established on first run (2026-06-22):
low fire risk, no flood risk, low AQI risk. Marine influence dominant.

### Containerization — built, node-01 migration pending

`docker-compose.yml` (repo root) containerizes the collectors, domain agents,
and ATProto publisher — everything except the PDS, which already had its own
compose file (`ATProto/pds/`). Two images: a shared one for River/Weather/AQI
(identical deps), a lighter one for the publisher (`httpx` only, no
`anthropic`/`mcp`). `node_config.json` and `.env` are bind-mounted rather than
baked in, so the built images are node-agnostic — deploying node-02 is
"clone, write new config, done," not "rebuild an image with different
hardcoding." Cron lines change from `.venv/bin/python script.py` to
`docker compose run --rm <service> python script.py` — same one-shot,
run-to-completion shape as Synthesis's Azure Container Apps Job, applied
node-side. See `DEPLOYMENT.md` for fresh-node setup and, more carefully, the
migration path off node-01's current venv+cron setup without losing its
existing SQLite history (the compose bind-mounts point at the same
`<Stack>/data/` paths the venv setup already writes to — no export/import,
just point cron at the new invocation once a manual test run confirms it
works).

Not yet cut over on node-01 — this exists as tested-but-unswapped capability,
same posture as anything else in this doc marked "built, not yet live."

---

## Environment

```
Pi OS: Raspberry Pi OS (Debian-based)
Python: 3.11+
All stacks use independent venvs at <stack>/.venv/
Environment variables set in /etc/environment, sourced in cron via `. /etc/environment`
```

Required environment variables:
- `ANTHROPIC_API_KEY` — used by all agents
- `AIRNOW_API_KEY` — used by AQI collector and agent
- `FIRMS_API_KEY` — used by Fire collector (NASA FIRMS `MAP_KEY`, free registration
  at https://firms.modaps.eosdis.nasa.gov/api/map_key/)
- `BSKY_HANDLE` / `BSKY_APP_PASSWORD` (Pi) — node's self-hosted PDS account
  credentials, e.g. `napa-node-01.watershed-agent.dev` — not a Bluesky app
  password despite the variable names (kept for continuity with the publisher's
  original bsky.social-based auth flow, which is unchanged, just pointed elsewhere)
- `ATPROTO_PDS_URL` (Pi only) — which PDS `ATProto/publisher.py` publishes to.
  Defaults to the self-hosted PDS; falls back to `bsky.social` if unset. Not
  used on Azure/by the subscriber — see "Synthesis agent" above and "DID
  resolution" below for why fetch-side PDS lookup is per-DID, not a single URL.

---

## Known issues / notes

- AirNow API occasionally returns empty responses — collector logs a warning and retries next poll. Normal behaviour, not a bug.
- USGS qualifiers are returned as plain strings not dicts — fixed in collector.py (parse_usgs_response).
- Weather and AQI venvs needed to be created separately from Watershed — each stack is fully independent.
- `/etc/environment` is not loaded automatically by cron — sourced explicitly with `. /etc/environment &&` prefix on each cron line.
- **ATProto records are DAG-CBOR — no float type exists in the data model.**
  Only `null, boolean, integer, string, cid, bytes, array, object` are valid.
  Any numeric field pulled from a SQLite `REAL` column must be stringified before
  going into a record, or `createRecord` rejects it with `InvalidRequest`. Fixed
  in `publisher.py` via `_atproto_safe()`; worth remembering for any future field.
- `PDS_HOSTNAME` alone does not authorize account handles under that domain on a
  self-hosted PDS — `PDS_SERVICE_HANDLE_DOMAINS` (suffix match, leading dot) is
  required too.
- Recent `bluesky-social/pds` images don't ship `pdsadmin` or `dist/scripts/
  create-account.js` inside the container — account creation is a plain
  `com.atproto.server.createAccount` XRPC call instead.
- `cloudflared`'s systemd service runs as root and doesn't see the invoking
  user's `~/.cloudflared` — config and credentials need to live under
  `/etc/cloudflared/`.
- DNSimple (and most registrars) can't delegate a single subdomain's NS records
  without moving the whole zone — Cloudflare Tunnel's cert-issuance flow needs a
  zone already on Cloudflare's nameservers. Registering a small dedicated domain
  directly through Cloudflare Registrar sidesteps this entirely (see below).

---

## Cron (current)

```cron
# === Collectors ===
*/15 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python collector.py >> logs/collector.log 2>&1

# === Domain Agents ===
0 0,6,12,18 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 1,7,13,19 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 3,9,15,21 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python agent.py >> logs/agent.log 2>&1

# === ATProto Publisher (15 min after the last domain agent — now Fire, 21h) ===
15 3,9,15,21 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/ATProto && .venv/bin/python publisher.py >> logs/publisher.log 2>&1
```

Synthesis runs as an Azure Container Apps Job (`0 6,18 * * *` UTC), not a Pi cron
entry — see "Synthesis agent" above.

---

## What's next

### Done
- [x] ATProto publisher — domain observations published as structured lexicon records
- [x] Synthesis subscriber — fetch-mode (cron-shaped, not firehose daemon), lookback window
- [x] Synthesis publisher — separate identity (`napasynth01.bsky.social`), advisory framing
- [x] End-to-end pipeline confirmed: Pi nodes → ATProto → Synthesis agent → advisory post
- [x] TRUSTED_PUBLISHERS / publishers.json in place as interim trust boundary (did:web registry replaces this)
- [x] Synthesis agent containerised and deployed to Azure Container Apps Job
- [x] Self-hosted PDS on the Pi (`napa-node-01.watershed-agent.dev`), fronted by a
      Cloudflare Tunnel — domain agents no longer depend on `bsky.social` for
      their own identity or data (2026-07-02)
- [x] Numeric fields (temperature, discharge, gage height, PM2.5/ozone AQI, etc.)
      now populated in lexicon records, joined from collector DBs at publish time
      (2026-07-02) — previously only `summary`/`flagged` were written
- [x] Trend-analysis field names in `agent_atproto.py` fixed to match what the
      publisher actually emits — was silently finding nothing (2026-07-02)
- [x] Containerization (docker-compose) for the domain stacks — built, node-01
      migration not yet cut over (2026-07-03)
- [x] Fourth domain added: Fire (NASA FIRMS satellite hotspot detection) —
      closes the "unidentified upwind fire source" gap Synthesis's own
      reasoning had repeatedly flagged (2026-07-06)

### Host Synthesis agent outside the laptop — DONE

The Synthesis agent is now containerised and deployable to **Azure Container Apps Jobs**,
replacing the laptop cron with a cloud-hosted, cron-scheduled run-to-completion job.

**What was built:**

| File | Purpose |
|------|---------|
| `Synthesis/requirements.txt` | Python dependencies (anthropic, httpx, atproto) |
| `Synthesis/Dockerfile` | Container image — copies subscriber, agent, publisher |
| `Synthesis/entrypoint.sh` | Pipeline script: subscriber → agent_atproto → publisher |
| `Synthesis/deploy/deploy.sh` | Azure CLI provisioning script (full infrastructure + image) |
| `Synthesis/deploy/job.yaml` | Container Apps Job spec template (image + volume mount) |

**Architecture:**
- Image built via `az acr build` — no local Docker daemon required
- SQLite databases persisted on an **Azure File Share** mounted at `/data`
  (subscriber.db, synthesis.db, synth_publisher.db)
- Secrets (`ANTHROPIC_API_KEY`, `BSKY_SYNTH_HANDLE`, `BSKY_SYNTH_APP_PASSWORD`)
  injected as Container Apps secrets — never stored in the image or YAML
- Managed identity granted `AcrPull` on the registry — no credential rotation needed
- Schedule: `0 6,18 * * *` UTC (matching existing twice-daily cadence)
- Pipeline runs to completion in under 10 min; job is killed after 600s if hung

**To deploy:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export BSKY_SYNTH_HANDLE=napasynth01.bsky.social
export BSKY_SYNTH_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
cd Synthesis/deploy && ./deploy.sh
```

**To trigger a manual run:**
```bash
az containerapp job start --name synthesis-agent --resource-group rg-agentic-watershed
```

This is also the first non-local execution of a registry-aware agent once the
did:web registry is ready — the Synthesis DID will be provisioned against the
cloud instance, not a developer laptop.

### Birthright identity for agents — the core research question

The deeper goal is an agent that has a **birthright identity**: a DID assigned at
creation that is its identity for life, independent of where it runs, who operates
it, or what infrastructure hosts it. ATProto's `did:plc` is the closest existing
primitive to this:

- The **DID is permanent** — not the handle, not the PDS URL, not the host
- The **DID document is mutable** — keys can rotate, PDS can move, identity persists
- **Data is bound to the DID**, not the PDS — move the PDS, the identity and its full
  history follow
- **Verification is decentralized** — any consumer can verify a signature against the
  DID document without trusting a platform or CA

What this enables: an agent can prove continuity of identity across time, machines,
operators, and infrastructure changes. "I am the same agent that made this observation
six months ago on a different machine" — provable from the DID chain alone.

This is fundamentally different from platform workload identity (SPIFFE, k8s
ServiceAccounts, Azure Managed Identity), which all require trusting the platform's
assertion. The DID model is self-sovereign — the agent carries its own verifiable
identity, and the platform is just where it happens to be running today.

**The open question:** how does a DID get established as trusted in the first place,
without reintroducing a centralized authority? The current `TRUSTED_PUBLISHERS` dict
is a hardcoded registry — that's the problem to solve. Options worth exploring:
- A trust registry published as ATProto records by a known authority DID
- Web-of-trust: a trusted DID vouches for a new DID
- Challenge/response at first contact: new node proves DID control before being added
- Self-describing agents: the DID document itself carries capability/scope claims

**Running our own PDS — DONE (2026-07-02).** Removed the dependency on `bsky.social`
as host while keeping full ATProto compatibility and DID portability. See "Current
deployment state" above and the "Architecture decisions" section below for why.

### DID onboarding problem — and the path to did:web

The node's DID is now `did:plc:ggztd5hjk3cnkhgzdk4rmqan`, minted by its own
self-hosted PDS (`napa-node-01.watershed-agent.dev`) rather than borrowed from a
Bluesky account signup — a real step forward, since the identity no longer depends
on Bluesky-the-company's infrastructure. Synthesis's DID
(`did:plc:clcw2dxrd6qma45gy3oozjwa` / `napasynth01.bsky.social`) is still
Bluesky-issued, appropriately — it's the one identity in this system with an
actual human-facing purpose.

**The onboarding problem itself is unchanged, though.** Both DIDs were still
bootstrapped by a person running an account-creation command by hand (`curl` to
`com.atproto.server.createAccount`, or Bluesky's signup flow) — an agent's
birthright identity shouldn't require a human to click through or type a command
at all. `did:plc` is also still a step short of `did:web`: it depends on a
third-party PLC directory (`plc.directory`, itself Bluesky-run) for resolution,
even though it no longer depends on Bluesky for hosting or the account layer.

**These DIDs are still effectively placeholders** for the eventual Agent Identity
Registry — the ATProto publishing pipeline stays intact regardless of which identity
system sits underneath. When the registry is ready, the reconciliation path is:

1. Registry mints new `did:web` DIDs for `napa-node-01` and `napasynth01`
2. Agents register their public keys and charters with the registry
3. `publishers.json` updated to the new DIDs
4. ATProto records going forward are signed by the registry-provisioned keys
5. The self-hosted PDS / Bluesky handle can remain the publishing transport —
   decoupled from the identity layer

The publishing target doesn't change. The identity primitive underneath does.
The Agent Identity Registry is being built in a separate repo — see that project for
the registry design and implementation.

### DID resolution — the plc.directory dependency we didn't remove

Self-hosting the PDS (above) removed the Bluesky *hosting* dependency: the node's
identity and data no longer live on infrastructure Bluesky operates. It did **not**
remove the Bluesky *resolution* dependency, and it's worth being precise about the
difference, because "distributed" in this project's framing has always meant
"mostly" — the honest state, not the aspirational one.

**How `did:plc` resolution actually works.** A `did:plc:...` string is an opaque
hash — unlike `did:web`, it does not encode where to resolve it. Every resolver
has to already know which directory to ask. In this codebase that's a hardcoded
constant, duplicated in two places since the Viewer (JS) and `subscriber.py`
(Python) share no runtime config:

```js
const PLC_DIRECTORY = "https://plc.directory";   // Viewer/index.html
```
```python
PLC_DIRECTORY = "https://plc.directory"          # Synthesis/subscriber.py
```

Querying `https://plc.directory/{did}` returns a DID document — a `service` array
with a `serviceEndpoint` telling you the DID's current PDS. That's the entire
mechanism: one public GET, one JSON field. It's what makes the "identity outlives
its hosting" property real — the DID is permanent, the PDS location is a mutable
field in a document the DID owner controls, and moving hosts requires zero
changes to anyone else's code, only an update to that document. `subscriber.py`
resolves each trusted publisher's DID independently rather than assuming they
share a PDS — the fix that actually makes multi-node fetch correct (see
"Synthesis agent" above); before that, a single global `PDS_HOST`/`ATPROTO_PDS_URL`
would have silently missed a second node's records if it ran its own PDS.

**What that document's integrity rests on.** Every operation in the PLC log —
create, rotate keys, move PDS, tombstone — is signed by the DID's own rotation
key, not merely attested to by whoever runs the directory. The server
(`did-method-plc`) is open source and the operation log is exportable, which is
a deliberate mitigation: the *data* isn't proprietary, so anyone holding a synced
copy could stand up a faithful replacement and serve the exact same mappings,
verifiable independent of who's hosting them.

**What isn't mitigated: resolution continuity.** There is no discovery mechanism
between directory instances — no DNS-style delegation, no fallback list, nothing
in the protocol that lets a resolver try a second directory if the first is
unreachable. In practice there is exactly one directory basically everyone in the
ATProto ecosystem resolves against, run by Bluesky PBC. If it went dark with no
warning:

- Every `did:plc` DID in the *entire ecosystem* — not just this project — becomes
  unresolvable for anyone without a cached copy, all at once. This is a systemic
  ATProto risk, not something specific to Agentic Watershed.
- Recovery depends on someone already running a synced mirror (or having exported
  the log pre-shutdown) standing up a replacement, and then every piece of client
  code in existence — including our one hardcoded constant — being manually
  repointed at it. Slow, uncoordinated, ecosystem-wide, not automatic failover.
- **New operations go dark before the mirror problem even matters.** Existing DID
  documents stay resolvable (read-only) as long as *some* copy of the log is
  served somewhere, but rotating keys or moving a PDS requires submitting a fresh
  signed operation to a directory actively accepting writes. With none, every
  `did:plc` identity is frozen at its last known state.

**Where this project stands on it, deliberately.** Not treating this as urgent —
the ATProto team is aware of this class of problem and it's a reasonable bet that
resolution federation gets addressed at the protocol level before it's forced by
an outage. But it's real, it's documented here rather than glossed over, and it
sharpens something already noted above: `did:web` resolves the discovery problem
*by construction* (the DID literally is the address — no shared directory at
all), which is the strongest concrete argument yet for eventually moving the
node's identity there. Two lower-effort mitigations worth doing before that, if
this becomes a live concern:

1. **Cache the last-resolved PDS URL** (Viewer, subscriber) instead of always
   live-resolving via `plc.directory` on every call — degrades a directory outage
   to "stale but working" instead of "broken."
2. **Consider self-hosting a PLC directory mirror** — `pds.env.example` already
   flags this as "possible but out of scope," which is still the right call for
   now, but it's the concrete next step if the shared dependency ever needs
   removing rather than just documenting.

### Watershed agent changes when the registry is ready

The agents need a small Python client module for the registry — three operations:

**`provision(charter) → DID`**
Generates a local keypair, registers the public key + charter with the registry,
returns the DID. Private key stored locally (`~/.agent/keys/{did}.pem`).
Called once at agent setup, not on every run.

**`sign(record) → signed_record`**
Signs an ATProto record with the agent's local private key before publishing.
Replaces the implicit signing via Bluesky App Password.

**`verify(did) → charter`**
Resolves a DID against the registry, returns its charter. Cached with TTL.
Used by `subscriber.py` to replace the static `TRUSTED_PUBLISHERS` dict.

The transition in `subscriber.py`:

```python
# Today — identity check only, static list
if publisher_did not in TRUSTED_PUBLISHERS:
    return

# After registry — identity + capability check, live lookup
charter = registry.verify(publisher_did)
if not charter or "observe" not in charter.capabilities:
    return
```

Trust becomes **capability-aware**, not just identity-aware. A DID that's known
to the registry but whose charter doesn't declare the `observe` capability is
rejected even if its identity is valid. This is the charter model in practice —
the registry doesn't just answer "who is this?" but "is this agent authorised
to do what it's claiming to do?"

**`did:web` is the near-term clean answer.** A DID document is just a JSON file served
at a well-known URL:

```
did:web:cpricedomain.net:agents:napanode01
  → https://cpricedomain.net/agents/napanode01/did.json
```

No signup flow. No human identity in the loop. Agent provisioning generates a keypair,
writes the DID document to the domain, done. Three lines of Python.

**The scaling problem:** `did:web` ties each DID to a URL path — one file (or route)
per agent. Fine at tens, unmanageable at thousands.

**What this wants to be: an Agent Identity Registry.**

A lightweight API at your domain that mints and manages DIDs for agents:

```
POST /agents              → generate keypair, mint DID, record charter → returns DID
GET  /agents/{id}/did.json → serve DID document (did:web resolution endpoint)
GET  /agents/{id}/charter  → serve the agent's charter (capabilities, scope, intent)
POST /agents/{id}/rotate   → rotate keys, update DID document
DELETE /agents/{id}        → revoke — DID document returns tombstone
```

**Implementation complexity: low.** Standard JWK keypairs, SQLite, a tiny FastAPI
app. A weekend project for the core. The hard questions are design:

- **Charter schema** — what capability claims, scope, intent, operator identity fields
  does a charter carry? Probably JSON-LD or a custom Lexicon.
- **Key custody** — the registry should never hold private keys. The agent generates
  its own keypair and registers only the public key. The registry issues the DID and
  records the charter. Closer to a CA than an IDP.
- **Provisioning policy** — who can mint a DID? Open self-service, or does the
  registry gatekeep? If the registry is a trust anchor, this matters.
- **Registry's own DID** — the registry itself should have a `did:web` at the domain
  root. Agent DID documents reference it as their controller/issuer. Consumers can
  verify: "this agent was provisioned by this trusted registry" — chain of trust
  without a central CA.

**This registry is the thing Ping should probably build.** It's an identity provider
for agents — but one that issues birthright DIDs and stores charters rather than
managing human sessions and issuing tokens. The registry's DID is the trust anchor;
the agent's DID document is the verifiable claim that it was provisioned by that anchor.

### Why this breaks the IDP model — and why that matters

The IDP model was designed for humans. It assumes trust is established by a person
logging in, consenting, and receiving a token from a central authority. That authority
is the source of truth for identity. Every token expires; every session ends; every
agent must re-authenticate through the same central chokepoint.

This breaks for agents at scale:
- Agents outnumber humans by orders of magnitude and operate continuously
- A central IDP is a single point of failure and a trust bottleneck
- Token lifetimes and refresh flows assume a human available to re-consent
- The IDP knows nothing about *what the agent has done* — only what it was granted

**The charter model replaces this entirely.** Each agent's DID document is its
charter — a self-describing declaration of identity, capability, and intent:

- **"I am"** — permanent DID, cryptographically verifiable, no authority required
- **"This is what I do"** — capability claims in the DID document (observe, synthesise, publish)
- **"This is my history"** — the full ATProto record chain, publicly auditable, bound to the DID
- **"This is what I want"** — the specific request, evaluated against all of the above

A policy engine receiving that bundle has everything needed to make an authorisation
decision: verified identity, declared scope, *and* a behavioural track record. It can
ask not just "was this agent granted access?" but "has this agent ever acted outside
its declared scope?" — a question no IDP token can answer.

This is dynamic trust based on verifiable identity plus observable behaviour over time.
Static grants (OAuth scopes, RBAC roles) are a degenerate case — useful when you know
nothing about the agent's history. When you have the chain, you can do much better.

The IDP doesn't disappear — it becomes one possible way to bootstrap initial trust.
But it is no longer the authority. The DID chain is.

### Where the IDP re-enters — delegated agent authorisation

The charter model handles agent-to-agent trust. But agents also need to act
on behalf of people — and that's where the IDP has a legitimate role.

The flow:

1. **Person authenticates** to an IDP (Ping, in this case) in the normal way
2. **Person delegates** to an agent DID for a specific scope:
   "I authorise `napasynth01` (DID: `did:plc:...`) to access my environmental
   data on MCP server X, for this purpose, for this duration"
   — that delegation is a verifiable credential, signed by the person's identity,
   referencing the agent's DID
3. **Agent presents** its DID + the delegation credential to the MCP AuthZ server
4. **AuthZ server issues a token** where:
   - `sub` = the person (the principal being acted on behalf of)
   - `act` = the agent DID (RFC 8693 Token Exchange — the actor)
   - scopes = what the agent is authorised to do on their behalf
5. **MCP server** validates the token, sees both the agent identity and the human
   principal, enforces policy against both

Revocation is clean: revoke the delegation credential. The agent's DID and charter
persist — it's just no longer authorised to act for that person. No token hunting,
no session invalidation. The credential chain is the audit trail.

This is OAuth 2.0 Token Exchange (RFC 8693) and Rich Authorization Requests
(RFC 9396) done with agent-native primitives — where the subject is a DID with
a charter and an observable history, not just an opaque client_id.

**The demo this points toward:** this watershed synthesis agent, with its DID,
its charter (declared in the DID document), and its public record of observations,
asks a Ping-protected MCP server for a token to act on behalf of a user. The
MCP AuthZ server (the one wired into this session) is exactly the thing that needs
to understand that exchange. The IDP handles the human side. The DID chain handles
the agent side. The AuthZ server holds them together.

This is the bridge between the agentic watershed work and Ping's core product.

### Possible additions
- Additional Pi nodes upvalley with their own domain agents and DIDs
- Physical sensors via Pi GPIO → same collector interface, no agent changes needed
- Additional USGS stations (Conn Creek, Milliken Creek tributaries)

### Next: move Synthesis agent from reactive to predictive

**The goal:** the agent should notice trends, anticipate risk windows, and eventually
compare its predictions against outcomes — building a track record over time.

Four phases, each buildable independently:

---

**Phase 1 — Better context ✅ DONE (2026-06-23)**

Shipped in `agent_atproto.py`:

- **Deeper memory**: `read_recent_synthesis()` now returns 14 observations (7 days of
  twice-daily runs) presented oldest-first as a timeline. The agent can now see weekly
  drift, not just recent state.

- **Seasonal calendar**: `seasonal_context()` function computes and injects the current
  date, fire season status (day N of 183), days until Diablo wind season onset, and
  flood season status into every prompt. Anchors the model in the actual calendar year.

- **Domain trajectories**: per-type observation cap increased from 5 → 6. Domain
  observations are trimmed of noise fields (publisher DID, agent model) so the token
  budget goes to signal.

- **Updated system prompt**: explicitly instructs the agent to reason about trajectory
  and flag developing trends even when current conditions are still benign.

---

**Phase 2 — Explicit trend calculation ✅ DONE (2026-06-23)**

Shipped in `agent_atproto.py`:

- `compute_trends()` function extracts numeric fields from `raw_record` JSON in
  `subscriber.db` for each domain and computes deltas across the observation window.

- Metrics tracked (field names match `ATProto/publisher.py`'s actual output as of
  2026-07-02 — the original field names here were speculative and never matched
  what got published; see "Known issues"):
  - Watershed: `dischargeCfs`, `gageHeightFt`
  - Weather: `temperature_f`, `humidity_pct`, `wind_speed_mph`, `precip_24h_mm`,
    `wind_direction_deg` (with Diablo quadrant detection)
  - AQI: `pm25Aqi`, `ozoneAqi`

- Each metric shows: old → new value, delta, hours elapsed, direction, and a plain-language
  risk note (e.g. *"humidity: -18% over 12h, falling — ⚠ fire risk building"*).

- Diablo wind detector: flags automatically when `windDirectionDeg` enters the 22°–112°
  NE/E quadrant with `⚠ DIABLO QUADRANT (NE/E offshore flow)`.

- Trends section is inserted after the domain observations in every prompt. If fewer than
  2 data points exist per domain, the section is omitted gracefully.

---

**Phase 3 — Prediction and outcome tracking ✅ DONE (2026-06-23)**

Prediction ledger is live. Key design decisions encoded as constants (not prompt-implied):

| Constant | Value | Meaning |
|---|---|---|
| `FLOOD_ACTION_STAGE_FT` | 12.0 ft | Gage height that confirms a flood prediction |
| `AQI_USG_THRESHOLD` | 100 | PM2.5 AQI that confirms an air quality prediction |
| `FIRE_CONFIRM_LEVELS` | `{high, extreme}` | `weather.fireRisk` values that confirm fire |
| `FIRE_CONFIRM_ALERTS` | Red Flag Warning, Fire Weather Watch | NWS alert names |
| Horizons | fire=48h, flood=72h, air_quality=24h | Auto-expiry windows |

`predictions` table added to `synthesis.db`. On each run:
1. `check_predictions()` runs at the **top** of `gather_context()` — resolves open
   predictions against current observations, marks expired ones `expired` (not left
   as `pending` indefinitely).
2. Compact ledger summary injected into every prompt (counts + 5 most recent).
3. `write_predictions()` called **before** `write_observation()` — ledger survives
   a crash between agent and publisher steps.

Also fixed in this session:
- `--lookback 13 → 15` in `entrypoint.sh` (safer overlap for clock drift/startup delay)
- `_is_diablo()`: full meteorological rationale in docstring (22°–112° not a magic number)
- System prompt: handles absent trends section explicitly; explains prediction ledger to agent

First real calibration test: Diablo wind season (September–November 2026).

**Known limitation — confirmation signals still use `flagged`, not numeric fields:**
Prediction resolution currently confirms via the domain agent's `flagged=True` field
rather than specific numeric thresholds (`FIRE_CONFIRM_LEVELS`, `FLOOD_ACTION_STAGE_FT`,
`AQI_USG_THRESHOLD`). This was originally deferred because the publisher only wrote
`summary`/`flagged` to ATProto records — **that's no longer true as of 2026-07-02**;
numeric fields (`temperature_f`, `dischargeCfs`, `gageHeightFt`, `pm25Aqi`, `ozoneAqi`,
etc.) are now populated on every record. Threshold-based confirmation in
`_resolve_prediction()` is a live option now, just not wired in yet — the constants
are still sitting unused in code, waiting for that change.

**What this project actually is:**
Agentic-Watershed is not primarily a fire/flood prediction system — prediction
accuracy is a secondary concern. The project is an exploration of distributed agent
architecture: how agents at the edge (Pi nodes) publish structured, DID-signed
observations; how a separate agent in the cloud (Azure) subscribes, reasons across
domains, and publishes advisories; and how identity, trust, and data flow across
that boundary without central coordination. The environmental domain is the vehicle,
not the destination.

---

**Phase 4 — Calibration (post-autumn)**

Once Phase 3 has accumulated a season of data:

- Track precision/recall by risk type and season.
- Feed a calibration summary into the system prompt.
- Seasonal recalibration: a separate monthly job updates the calibration summary
  on the Azure File Share, read by the synthesis agent on each run.

*Changes needed:* calibration job (new cron), calibration summary file on Azure
File Share, system prompt update to include it.

---

## Architecture decisions made

**Why separate venvs per stack?** Independence — each stack can be updated,
restarted, or replaced without affecting the others.

**Why MCP servers as stdio subprocesses?** Simplicity at this scale. Each
agent spawns the MCP server per tool call rather than running it persistently.
Switching to persistent HTTP is one flag (`--http`) when needed.

**Why does Synthesis use ATProto instead of reading SQLite directly?**
The Synthesis agent runs on a separate machine from the Pi nodes — it has no access
to their local SQLite databases. ATProto is the message bus: domain agents publish
structured observations as lexicon records, Synthesis subscribes via
`com.atproto.repo.listRecords` (fetch mode, cron-triggered), and reasons across
whatever it finds. This also decouples the agents completely — a node can be replaced,
moved, or added without any change to Synthesis. The subscriber verifies author DIDs
against `publishers.json` (interim trust registry) before accepting records.

**Why a self-hosted PDS instead of publishing to `bsky.social`?**
The original motivation was Bluesky (a place to see posts); the actual interest is
ATProto — portable identity, federation, data bound to the DID rather than the
platform. Staying on `bsky.social` meant the node's identity was, in practice,
Bluesky-the-company's to revoke or rate-limit. Running the official PDS on the Pi
means the node's identity depends only on the protocol, not on a specific operator's
infrastructure — closer to the "A Half-Built Garden" framing than borrowing Bluesky's
implicit trust layer ever was. It also cleanly separates concerns: domain agents write
structured records for other agents to consume (no `app.bsky.feed.post` needed);
only Synthesis, which has an actual human audience, still touches the public network.

**Why register a separate domain (`watershed-agent.dev`) instead of using the existing `cpricedomain.net`?**
Cloudflare Tunnel's cert-issuance flow (`cloudflared tunnel login`) requires a zone
already on Cloudflare's nameservers — there's no way to delegate just one subdomain's
NS records without moving the whole zone, and DNSimple (where `cpricedomain.net`
lives) doesn't support that either. Rather than touch a domain other things depend
on, registering a small dedicated domain directly through Cloudflare Registrar meant
DNS was authoritative on Cloudflare from the moment of registration — no delegation,
no propagation wait — and it separates "Agentic Watershed infrastructure identity"
from the personal domain, which also fits the project's own theme.

**Why Cloudflare Tunnel instead of port-forwarding on the home router?**
Residential ISPs increasingly put connections behind CGNAT, which makes inbound
port-forwarding impossible regardless of router configuration — and even where it
works, it means running a public TLS endpoint on a home network with no DDoS
protection and an IP that can't be rotated without breaking the DID's service
endpoint. A tunnel makes only an outbound connection from the Pi; no inbound ports
are ever opened, it works regardless of CGNAT, and Cloudflare terminates TLS.

**Why Sonnet for Synthesis, Haiku for domain agents?**
Cross-domain reasoning across multiple observation sets warrants more capability.
Domain agents do single-domain threshold assessment — Haiku is sufficient and cheaper.

**Why are agents cron-triggered rather than long-running?**
Simpler, more robust, easier to debug. A failed run doesn't affect the next one.
Statelessness is a feature — memory is explicit via the observations tables.
