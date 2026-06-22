# Project Context — Agentic Watershed

Living document. Update this as the project evolves so coding agents and
collaborators can pick up where things left off without needing the full
conversation history.

Last updated: 2026-06-22

---

## Distributed architecture intent

This is a **distributed multi-agent system**. The design separates node agents from the
synthesis agent both logically and physically:

- **Node agents** (Raspberry Pi): collect domain data, reason locally with Claude Haiku,
  publish structured observations to ATProto/Bluesky using a custom lexicon
  (`com.napavalley.monitor.observation`) and a per-node verified DID.

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
| 6h, 18h | ✅ Running live (not dry-run), writing to `synthesis.db` |

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

### Immediate
- [ ] Let data accumulate (2-3 days) before evaluating synthesis quality
- [ ] Monitor synthesis observations for cross-domain reasoning quality

### Next feature: ATProto publisher (closes the distributed loop)

This is the central next step — it completes the distributed architecture:

1. **Register ATProto DIDs** — one DID per domain agent (Watershed, Weather, AQI)
   and a separate DID for the Synthesis agent. These are workload identities.

2. **Build the domain publisher** — a separate process per stack that reads
   `agent_observations` from local SQLite and posts new records to ATProto as
   `com.napavalley.monitor.observation`. Runs after each domain agent cron job.

3. **Design the lexicon** — `com.napavalley.monitor.observation` fields map to
   the existing synthesis output schema. The lexicon is what makes records
   machine-readable and filterable on the firehose.

4. **Build the firehose subscriber** — replaces Synthesis's SQLite-direct reads.
   Subscribes to the ATProto firehose, filters by lexicon, verifies author DIDs
   against a trusted registry, accumulates observations, triggers Synthesis reasoning.

5. **Trusted DID registry** — a simple list of known node DIDs that Synthesis
   will accept observations from. The trust boundary for the distributed system.

The `summary` field in synthesis output is already written for a public Bluesky post —
high-risk events can surface as human-readable posts in addition to structured records.

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

### Possible additions
- Additional Pi nodes upvalley with their own domain agents and DIDs
- Physical sensors via Pi GPIO → same collector interface, no agent changes needed
- Additional USGS stations (Conn Creek, Milliken Creek tributaries)

---

## Architecture decisions made

**Why separate venvs per stack?** Independence — each stack can be updated,
restarted, or replaced without affecting the others.

**Why MCP servers as stdio subprocesses?** Simplicity at this scale. Each
agent spawns the MCP server per tool call rather than running it persistently.
Switching to persistent HTTP is one flag (`--http`) when needed.

**Why does Synthesis read SQLite directly instead of via MCP (currently)?**
Domain agent observations are already clean, structured conclusions — no need for
the MCP abstraction layer when reading conclusions rather than raw data.
This is a *prototype convenience*, not the target architecture. When Synthesis moves
to a separate device, it will subscribe to the ATProto firehose instead of reading SQLite.
The firehose subscriber is the planned replacement for the SQLite-direct reads.

**Why Sonnet for Synthesis, Haiku for domain agents?**
Cross-domain reasoning across multiple observation sets warrants more capability.
Domain agents do single-domain threshold assessment — Haiku is sufficient and cheaper.

**Why are agents cron-triggered rather than long-running?**
Simpler, more robust, easier to debug. A failed run doesn't affect the next one.
Statelessness is a feature — memory is explicit via the observations tables.
