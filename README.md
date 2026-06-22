# Agentic Watershed — Napa Valley Environmental Monitor

An autonomous multi-agent environmental monitoring system running on a Raspberry Pi 5.
Monitors the Napa River, local weather, and air quality — and reasons across all three
to produce cross-domain risk assessments, published to Bluesky via ATProto.

Built as a practical exploration of non-conversational agentic architecture:
autonomous agents that perceive, reason, and act without a human in the loop.

---

## Architecture

```
[USGS API]     → [Watershed Collector] → [watershed.db]
[NWS API]      → [Weather Collector]   → [weather.db]
[AirNow API]   → [AQI Collector]       → [aqi.db]
                         ↓
              [Domain MCP Servers]
              (watershed / weather / aqi)
                         ↓
              [Domain Agents] (Claude Haiku)
              Each reasons over its own domain,
              writes structured conclusions to DB
                         ↓
              [Synthesis Agent] (Claude Sonnet)
              Reads domain conclusions,
              reasons across all three,
              produces unified risk assessment
                         ↓
              [Bluesky Publisher] (planned)
              Posts to ATProto when flagged
```

### Key design principles

**Separation of concerns** — collectors know nothing about reasoning; agents know nothing about how data is stored. The MCP boundary separates perception from cognition.

**Stateless agents, persistent memory** — each agent run is independent. Memory is explicit: agents read prior `agent_observations` from the DB at the start of each run, and write new ones at the end. The next run reads these conclusions, not raw sensor data.

**Conclusions, not data** — the synthesis agent reads what each domain agent *concluded*, not the underlying readings. Domain agents are specialists; the synthesiser reads their reports.

**No conversation** — no human in the loop, no chat interface. Agents are cron-triggered, run to completion, and exit.

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

## Planned: ATProto / Bluesky publisher

When `flagged=true`, the synthesis agent's `summary` will be posted
to Bluesky using a custom lexicon:

```
com.napavalley.monitor.observation
```

Fields will map to the synthesis output schema, making observations
queryable and federable beyond just a text post.

---

## Future: Distributed identity

Currently all agents run on a single Pi and share a filesystem.
When distributed across nodes, each domain MCP server would run in HTTP mode
and the synthesis agent would call tools over the network.

This immediately surfaces workload identity questions:
- How does the synthesis agent prove it's authorised to query domain agents?
- How do domain agents verify the caller before releasing conclusions?
- What happens when a node's identity is revoked mid-run?

The architecture is designed to make this transition natural —
the MCP tool contract is identical whether the transport is stdio or HTTPS.
Candidate approaches: SPIFFE/SVID per node, charter-based authorisation,
PKI/X.509 workload certificates.

---

## Author

Chris Price — [@cpricedomain.net](https://bsky.app/profile/cpricedomain.net)  
Distinguished Sales Engineer, Ping Identity  
Napa, California
