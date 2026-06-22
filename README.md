# Agentic Watershed — Napa Valley Environmental Monitor

An autonomous multi-agent environmental monitoring system for Napa Valley.
Monitors the Napa River, local weather, and air quality — and reasons across all three
to produce cross-domain risk assessments.

Built as a practical exploration of non-conversational agentic architecture:
autonomous agents that perceive, reason, and act without a human in the loop.

**Design intent: distributed from the start.** Node agents run on a Raspberry Pi,
reason locally with Claude Haiku, and publish structured observations to ATProto/Bluesky
using a custom lexicon. A Synthesis agent — intended to run on a *separate machine* —
subscribes to the ATProto firehose, filters by that lexicon, verifies publisher DIDs
against a trusted registry, and reasons across domains with Claude Sonnet.
ATProto is the message bus, not just the output channel.

The current deployment runs Synthesis on the same Pi and reads SQLite directly —
a working prototype before the distributed pieces are in place.

---

## Architecture

```
                    ┌─────────────── Node (Raspberry Pi) ───────────────┐
                    │                                                     │
[USGS API]  → [Watershed Collector] → [watershed.db]                    │
[NWS API]   → [Weather Collector]   → [weather.db]                      │
[AirNow API]→ [AQI Collector]       → [aqi.db]                          │
                         ↓                                               │
              [Domain MCP Servers]                                       │
              (watershed / weather / aqi)                                │
                         ↓                                               │
              [Domain Agents] (Claude Haiku)                            │
              Each reasons over its own domain,                          │
              writes structured conclusions,                             │
              publishes to ATProto/Bluesky                              │
              using custom lexicon + verified DID                       │
                    └──────────────────────┬──────────────────────────┘
                                           │ ATProto firehose
                    ┌──────────────────────▼──────────────────────────┐
                    │           Synthesis Machine (separate)           │
                    │                                                   │
                    │  [Synthesis Agent] (Claude Sonnet)               │
                    │  Subscribes to firehose                          │
                    │  Filters by custom lexicon                       │
                    │  Verifies publisher DIDs against trusted registry│
                    │  Reasons across domains                          │
                    │  Produces unified risk assessment                │
                    └───────────────────────────────────────────────┘
```

### Current deployment (prototype)

Synthesis runs on the same Pi and reads SQLite directly — skipping the ATProto
transport until the publisher is built. The architecture is otherwise identical:
domain agents write structured conclusions, Synthesis reads them and reasons across all three.

### Key design principles

**ATProto as message bus** — domain agent observations are published as structured ATProto
records using a custom lexicon (`com.napavalley.monitor.observation`). The Synthesis agent
is a subscriber, not a database reader. This makes the system federable and the agents
genuinely independent.

**DID-based trust** — each publishing agent has an ATProto DID. The Synthesis agent
verifies incoming records against a trusted DID registry before acting on them.
A compromised or spoofed node is rejected at the boundary.

**Separation of concerns** — collectors know nothing about reasoning; agents know nothing
about how data is stored. The MCP boundary separates perception from cognition.

**Stateless agents, persistent memory** — each agent run is independent. Memory is explicit:
agents read prior `agent_observations` from the DB at the start of each run, and write new
ones at the end. The next run reads these conclusions, not raw sensor data.

**Conclusions, not data** — the synthesis agent reads what each domain agent *concluded*,
not the underlying readings. Domain agents are specialists; the synthesiser reads their reports.

**No conversation** — no human in the loop, no chat interface. Agents are cron-triggered,
run to completion, and exit.

---

## Stack layout

```
Agentic-Watershed/
  watershed/
    collector/collector.py    USGS stream gauge → SQLite
    mcp_server/mcp_server.py  MCP tools over watershed.db
    agent/agent.py            Domain agent (Haiku)
    README.md
  weather/
    collector/collector.py    NWS observations + alerts → SQLite
    mcp_server/mcp_server.py  MCP tools over weather.db
    agent/agent.py            Domain agent (Haiku)
    README.md
  aqi/
    collector/collector.py    AirNow PM2.5/Ozone → SQLite
    mcp_server/mcp_server.py  MCP tools over aqi.db
    agent/agent.py            Domain agent (Haiku)
    README.md
  synthesis/
    agent/agent.py            Cross-domain agent (Sonnet)
    README.md
```

---

## Data sources

| Stack | Source | Auth |
|-------|--------|------|
| Watershed | [USGS Water Services](https://waterservices.usgs.gov/) | None |
| Weather | [NWS API](https://api.weather.gov) | None (User-Agent required) |
| AQI | [AirNow API](https://docs.airnowapi.org/) | Free API key |

USGS stations monitored:
- `11458000` — Napa River near Napa
- `11456000` — Napa River near St Helena

NWS alert zones:
- `CAZ505` — Napa County interior valleys (fire weather)
- `CAC055` — Napa County general

---

## Setup

### Prerequisites

- Raspberry Pi 5 (or any Linux system)
- Python 3.11+
- Anthropic API key
- AirNow API key (free)

### Per-stack setup

Each stack has its own virtual environment:

```bash
cd ~/Agentic/<Stack>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # or see stack README for deps
```

Dependencies:
- Watershed, Weather, AQI: `anthropic httpx "mcp>=1.27,<2"`
- Synthesis: `anthropic` only

### Environment variables

```bash
# Add to /etc/environment on Pi (cron picks these up via `. /etc/environment`)
ANTHROPIC_API_KEY=sk-ant-...
AIRNOW_API_KEY=your-key
```

See `.env.example` for reference.

---

## Cron schedule

```cron
# Source env vars for all jobs
# === Collectors ===
*/15 * * * * . /etc/environment && cd /home/cprice/Agentic/Watershed && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic/Weather && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic/AQI && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1

# === Domain Agents (staggered by 1h) ===
0 0,6,12,18 * * * . /etc/environment && cd /home/cprice/Agentic/Watershed && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
0 1,7,13,19 * * * . /etc/environment && cd /home/cprice/Agentic/Weather && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
0 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic/AQI && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1

# === Synthesis Agent (twice daily, after morning/evening domain runs) ===
0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
```

---

## MCP Servers

Each domain stack exposes its data as MCP tools. The MCP servers run as
stdio subprocesses spawned by the agent — not as persistent services.
This keeps the architecture simple while preserving the MCP tool contract.

To inspect tools interactively:
```bash
python mcp_server/mcp_server.py --http --port 8000
# Open MCP Inspector → connect to http://localhost:8000/mcp
```

HTTP port assignments: Watershed=8000, Weather=8001, AQI=8002.

---

## Synthesis output schema

Each synthesis run produces a structured observation:

```json
{
  "summary": "Plain language, Bluesky-ready, 3-4 sentences",
  "fire_risk": "none|low|moderate|high|extreme",
  "flood_risk": "none|low|moderate|high|extreme",
  "air_quality_risk": "none|low|moderate|high|extreme",
  "overall_risk": "none|low|moderate|high|extreme",
  "flagged": true,
  "flag_reason": "Brief reason if flagged",
  "reasoning": "Full cross-domain reasoning"
}
```

---

## Next: ATProto publisher (the real message bus)

Domain agents will publish observations to ATProto/Bluesky using a custom lexicon:

```
com.napavalley.monitor.observation
```

This is not just a posting mechanism — it's how the Synthesis agent receives
domain observations in the distributed design. Fields map directly to the
synthesis output schema, making observations queryable and federable.

The publisher is a separate process per domain stack that:
1. Reads `agent_observations` from the local SQLite DB
2. Posts new (unflagged-for-publish) records to ATProto as `com.napavalley.monitor.observation`
3. Uses the domain agent's registered DID as the author identity

---

## Distributed identity

Each node (Pi running domain agents) will have a registered ATProto DID.
The Synthesis agent — running on a separate machine — subscribes to the firehose
and filters for `com.napavalley.monitor.observation` records.

Before reasoning on any record, Synthesis verifies the author DID against a
trusted registry. This is the workload identity boundary: a record from an
unrecognised DID is discarded, not reasoned on.

This surfaces interesting identity questions that connect to Ping Identity's work:
- How does a node prove it's an authorised publisher?
- How is the trusted DID registry maintained and updated?
- What happens when a node's DID is revoked mid-run?
- Can a compromised node publish plausible-looking records that fool Synthesis?

The MCP servers already support HTTP mode (`--http` flag) for when domain agent
tools need to be called across the network. Candidate workload identity approaches:
SPIFFE/SVID per node, charter-based authorisation, PKI/X.509 certificates.

---

## Author

Chris Price — [@cpricedomain.net](https://bsky.app/profile/cpricedomain.net)  
Distinguished Sales Engineer, Ping Identity  
Napa, California
