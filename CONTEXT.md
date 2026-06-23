# Project Context — Agentic Watershed

Living document. Update this as the project evolves so coding agents and
collaborators can pick up where things left off without needing the full
conversation history.

Last updated: 2026-06-23

---

## Distributed architecture intent

This is a **distributed multi-agent system**. The design separates node agents from the
synthesis agent both logically and physically:

- **Node agents** (Raspberry Pi): collect domain data, reason locally with Claude Haiku,
  publish structured observations to ATProto/Bluesky using a custom lexicon
  (`net.cpricedomain.temp.monitor.observation`) and a per-node verified DID.

- **Synthesis agent** (separate machine): subscribes to the ATProto firehose, filters
  records by the custom lexicon, verifies publisher DIDs against a trusted registry,
  and reasons across domains with Claude Sonnet.

ATProto is the message bus between nodes, not just a publishing endpoint.
The DID registry is the trust boundary — Synthesis only acts on observations from
known, trusted node identities.

**Current state: local prototype.** Synthesis runs on the same Pi and reads SQLite
directly. This is a working stand-in until the ATProto publisher and firehose subscriber
are built. The architecture is otherwise faithful to the distributed intent.

---

## Current deployment state

Running on a Raspberry Pi 5, Napa, California.
All stacks deployed under `/home/cprice/Agentic/`.

### Collectors — all running via cron

| Stack | Frequency | Status |
|-------|-----------|--------|
| Watershed | every 15 min | ✅ Running, storing to `watershed.db` |
| Weather | every 30 min | ✅ Running, storing to `weather.db` |
| AQI | every 30 min | ✅ Running, storing to `aqi.db` |

### Domain agents — all running via cron

| Stack | Schedule | Status |
|-------|----------|--------|
| Watershed | 0,6,12,18h | ✅ Running, writing observations |
| Weather | 1,7,13,19h | ✅ Running, writing observations |
| AQI | 2,8,14,20h | ✅ Running, writing observations |

### Synthesis agent

| Schedule | Status |
|----------|--------|
| 6h, 18h UTC | ✅ Running in Azure Container Apps Job (`synthesis-agent`, `rg-agentic-watershed`, `westus2`) |

The Synthesis agent now runs in Azure, not on the laptop. Pi (edge) → ATProto → Azure (cloud) → Bluesky.
First cloud run confirmed 2026-06-23 17:33 UTC. Advisory posts visible at `napasynth01.bsky.social`.

Accumulating domain observations — first meaningful cross-domain synthesis
expected after 2-3 days of data. Baseline established on first run (2026-06-22):
low fire risk, no flood risk, low AQI risk. Marine influence dominant.

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

---

## Known issues / notes

- AirNow API occasionally returns empty responses — collector logs a warning and retries next poll. Normal behaviour, not a bug.
- USGS qualifiers are returned as plain strings not dicts — fixed in collector.py (parse_usgs_response).
- Weather and AQI venvs needed to be created separately from Watershed — each stack is fully independent.
- `/etc/environment` is not loaded automatically by cron — sourced explicitly with `. /etc/environment &&` prefix on each cron line.

---

## Cron (current)

```cron
# === Collectors ===
*/15 * * * * . /etc/environment && cd /home/cprice/Agentic/Watershed && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic/Weather && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic/AQI && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1

# === Domain Agents ===
0 0,6,12,18 * * * . /etc/environment && cd /home/cprice/Agentic/Watershed && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
0 1,7,13,19 * * * . /etc/environment && cd /home/cprice/Agentic/Weather && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
0 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic/AQI && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1

# === Synthesis Agent ===
0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
```

---

## What's next

### Done
- [x] ATProto publisher — domain observations published to Bluesky as structured lexicon records
- [x] Synthesis subscriber — fetch-mode (cron-shaped, not firehose daemon), lookback window
- [x] Synthesis publisher — separate identity (`napasynth01.bsky.social`), advisory framing
- [x] End-to-end pipeline confirmed: Pi nodes → ATProto → Synthesis agent → advisory post
- [x] TRUSTED_PUBLISHERS dict in place as interim trust boundary (did:web registry replaces this)
- [x] Synthesis agent containerised and deployable to Azure Container Apps Job

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

**Running our own PDS** is the next infrastructure step — removes the dependency on
`bsky.social` as host while keeping full ATProto compatibility and DID portability.

### DID onboarding problem — and the path to did:web

The current agent DIDs (`napanode1.bsky.social`, `napasynth01.bsky.social`) were
bootstrapped through Bluesky's human signup flow. That's wrong — an agent's birthright
identity shouldn't require a person to click through an onboarding UI.

**These DIDs are placeholders.** The system continues to publish to Bluesky using
them — the ATProto publishing pipeline stays intact. When the Agent Identity Registry
is ready, the reconciliation path is:

1. Registry mints new `did:web` DIDs for `napanode01` and `napasynth01`
2. Agents register their public keys and charters with the registry
3. `TRUSTED_PUBLISHERS` in `subscriber.py` updated to the new DIDs
4. ATProto records going forward are signed by the registry-provisioned keys
5. Bluesky handles (`napanode1.bsky.social`, `napasynth01.bsky.social`) can remain
   as the human-readable publishing accounts — decoupled from the identity layer

The publishing target (Bluesky) doesn't change. The identity primitive underneath does.
The Agent Identity Registry is being built in a separate repo — see that project for
the registry design and implementation.

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

- Metrics tracked:
  - Watershed: `dischargeMeanCfs`, `gageHeightMaxFt`
  - Weather: `temperatureF`, `humidityPct`, `windSpeedMph`, `precipMm24h`,
    `windDirectionDeg` (with Diablo quadrant detection), `windPattern` (categorical change)
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
against `TRUSTED_PUBLISHERS` (interim) before accepting records.

**Why Sonnet for Synthesis, Haiku for domain agents?**
Cross-domain reasoning across multiple observation sets warrants more capability.
Domain agents do single-domain threshold assessment — Haiku is sufficient and cheaper.

**Why are agents cron-triggered rather than long-running?**
Simpler, more robust, easier to debug. A failed run doesn't affect the next one.
Statelessness is a feature — memory is explicit via the observations tables.
