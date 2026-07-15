# Agentic Watershed

A distributed system of autonomous agents connected by identity and a federated protocol.

The domain is Napa Valley environmental data — watershed, weather, air quality,
and satellite fire detection. That's the concrete surface. The actual subject
is the architecture:

- **Autonomous agents at the edge** — cron-triggered, no human in the loop, perceive → reason → publish → exit
- **DID-based workload identity** — each node agent has an ATProto DID, minted by its own self-hosted PDS rather than borrowed from a Bluesky account; the Synthesis agent verifies it before acting on any record. A spoofed or compromised node is rejected at the boundary, not silently trusted.
- **A federated protocol as the message bus, not Bluesky as the destination** — agents don't share filesystems or databases. They communicate via ATProto records using a custom lexicon, published to the node's own PDS. Only the Synthesis agent — the one component with an actual human audience — posts to the public Bluesky network.
- **Conclusions, not data** — domain agents publish what they *concluded*, not raw readings. The Synthesis agent reads their reports, not their sensors.

The environmental monitoring problem is well-suited because it has real data sources, genuine multi-domain reasoning, and seasonal risk patterns worth tracking — but the patterns here (edge agent → structured record → verified identity → synthesis) apply anywhere.

---

## Architecture

```
                    ┌─────────────── Node (Raspberry Pi) ───────────────┐
                    │                                                     │
[USGS API]  → [Watershed Collector] → [watershed.db]                    │
[NWS API]   → [Weather Collector]   → [weather.db]                      │
[AirNow API]→ [AQI Collector]       → [aqi.db]                          │
[NASA FIRMS]→ [Fire Collector]      → [fire.db]                         │
                         ↓                                               │
              [Domain MCP Servers]                                       │
              (watershed / weather / aqi / fire)                         │
                         ↓                                               │
              [Domain Agents] (Claude Haiku)                            │
              Each reasons over its own domain,                          │
              writes structured conclusions,                             │
              publishes lexicon records to the node's own                │
              self-hosted PDS — no Bluesky app post, no                  │
              dependency on bsky.social for identity or data             │
                    └──────────────────────┬──────────────────────────┘
                                           │ com.atproto.repo.listRecords
                                           │ (fetch mode, cron-triggered)
                    ┌──────────────────────▼──────────────────────────┐
                    │      Synthesis Agent (Azure Container Apps Job)  │
                    │                                                   │
                    │  [Synthesis Agent] (Claude Sonnet)               │
                    │  Fetches from the node's self-hosted PDS         │
                    │  Filters by custom lexicon                       │
                    │  Verifies publisher DIDs against trusted registry│
                    │  Reasons across domains, tracks predictions      │
                    │  Posts the human-facing advisory to Bluesky      │
                    │  (napasynth01.bsky.social) — the only step a     │
                    │  person actually sees                            │
                    └───────────────────────────────────────────────┘
```

### Current deployment

Pi nodes (edge) → self-hosted PDS (Cloudflare Tunnel, `watershed-agent.dev`) →
Synthesis agent (Azure Container Apps Job) → Bluesky advisory.

Domain agents publish structured lexicon records to their own PDS on the Pi —
not to Bluesky. Synthesis fetches via `com.atproto.repo.listRecords` against
that PDS, verifies publisher DIDs, reasons across domains with Claude Sonnet,
and posts advisories from `napasynth01.bsky.social`. The full pipeline is live;
see `ATProto/pds/README.md` for the PDS setup and `CONTEXT.md` for the
architecture decisions behind self-hosting it.

### Key design principles

**ATProto as message bus, not Bluesky as destination** — domain agent observations
are published as structured ATProto records using a custom lexicon
(`net.cpricedomain.temp.monitor.observation`) to the node's own self-hosted PDS.
The Synthesis agent is a subscriber, not a database reader, and the only component
that also posts to the public Bluesky network. This makes the system federable
and the agents genuinely independent — of each other, and of any single platform.

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
run to completion, and exit. The environmental domain makes this natural: weather doesn't
wait for a prompt.

---

## Stack layout

```
Agentic-Watershed/
  River/
    collector.py               USGS stream gauge → SQLite
    mcp_server.py               MCP tools over watershed.db
    agent.py                    Domain agent (Haiku)
    README.md
  Weather/
    collector.py                NWS observations + alerts → SQLite
    mcp_server.py                MCP tools over weather.db
    agent.py                     Domain agent (Haiku)
    README.md
  AQI/
    collector.py                 AirNow PM2.5/Ozone → SQLite
    mcp_server.py                 MCP tools over aqi.db
    agent.py                      Domain agent (Haiku)
    README.md
  Fire/
    collector.py                 NASA FIRMS satellite hotspots → SQLite
    mcp_server.py                 MCP tools over fire.db
    agent.py                      Domain agent (Haiku)
    README.md
  ATProto/
    publisher.py                  Publishes domain records to the node's PDS
    pds/                           Self-hosted PDS: docker-compose + setup docs
  Synthesis/
    agent/agent_atproto.py         Cross-domain agent (Sonnet)
    subscriber.py                  Fetches records from the node's PDS
    publisher.py                   Posts the human-facing advisory to Bluesky
    deploy/deploy.sh               Azure Container Apps Job deployment
    README.md
```

---

## Data sources

| Stack | Source | Auth |
|-------|--------|------|
| Watershed | [USGS Water Services](https://waterservices.usgs.gov/) | None |
| Weather | [NWS API](https://api.weather.gov) | None (User-Agent required) |
| AQI | [AirNow API](https://docs.airnowapi.org/) | Free API key |
| Fire | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) | Free API key (`MAP_KEY`) |

USGS stations monitored:
- `11458000` — Napa River near Napa
- `11456000` — Napa River near St Helena

NWS alert zones:
- `CAZ505` — Napa County interior valleys (fire weather)
- `CAC055` — Napa County general

FIRMS bounding box (configured in `node_config.json`'s `"fire"` block, not
hardcoded): `-123.3,37.7,-121.8,39.0` — roughly Napa/Sonoma/Solano/Lake
counties, sized for regional smoke-transport awareness, not just
county-line fires. Polls all three current VIIRS satellites (SNPP, NOAA-20,
NOAA-21), not just one — each has its own overpass schedule, and
single-source polling missed a real, active fire for days before this
was caught.

---

## Setup

### Prerequisites

- Raspberry Pi 5 (or any Linux system), or any machine for a new node
- Docker + Compose plugin (`docker compose version`) — the supported path
- Anthropic API key
- AirNow API key (free)

### Docker Compose (recommended)

```bash
cp .env.example .env      # fill in real values
docker compose build
```

Each service is invoked one-shot via cron (`docker compose run --rm <service> ...`),
not run as a long-lived daemon — see "Cron schedule" below. `node_config.json`
and `.env` are bind-mounted, not baked into the image, so the same build works
for any node — just point cron at a different checkout with its own config.
Deploying a new node, or migrating an existing venv+cron setup over without
losing history: see `DEPLOYMENT.md`.

### Without Docker (legacy / alternative)

Each stack has its own virtual environment:

```bash
cd ~/Agentic-Watershed/<Stack>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # or see stack README for deps
```

Dependencies:
- River, Weather, AQI, Fire: `anthropic httpx "mcp>=1.27,<2"`
- ATProto, Synthesis: `anthropic httpx` (no MCP — these talk ATProto, not stdio tools)

### Environment variables

```bash
# Docker: .env at repo root (see .env.example)
# Non-Docker: /etc/environment on Pi (cron picks these up via `. /etc/environment`)
ANTHROPIC_API_KEY=sk-ant-...
AIRNOW_API_KEY=your-key
FIRMS_API_KEY=your-firms-map-key   # https://firms.modaps.eosdis.nasa.gov/api/map_key/

# ATProto publisher — node's self-hosted PDS account (see ATProto/pds/README.md)
BSKY_HANDLE=napa-node-01.watershed-agent.dev
BSKY_APP_PASSWORD=...
ATPROTO_PDS_URL=https://napa-node-01.watershed-agent.dev
```

---

## Cron schedule

Docker Compose (recommended — run from the repo root):

```cron
# === Collectors ===
*/15 * * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm river python collector.py >> River/logs/collector.log 2>&1
*/30 * * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm weather python collector.py >> Weather/logs/collector.log 2>&1
*/30 * * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm aqi python collector.py >> AQI/logs/collector.log 2>&1
*/30 * * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm fire python collector.py >> Fire/logs/collector.log 2>&1

# === Domain Agents (staggered by 1h) ===
0 0,6,12,18 * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm river python agent.py >> River/logs/agent.log 2>&1
0 1,7,13,19 * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm weather python agent.py >> Weather/logs/agent.log 2>&1
0 2,8,14,20 * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm aqi python agent.py >> AQI/logs/agent.log 2>&1
0 3,9,15,21 * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm fire python agent.py >> Fire/logs/agent.log 2>&1

# === ATProto Publisher (15 min after the last domain agent, now Fire) ===
15 3,9,15,21 * * * cd /home/cprice/Agentic-Watershed && docker compose run --rm atproto-publisher python publisher.py >> ATProto/logs/publisher.log 2>&1
```

Without Docker (legacy — same schedule, `.venv/bin/python` instead of `docker compose run`):

```cron
# === Collectors ===
*/15 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python collector.py >> logs/collector.log 2>&1

# === Domain Agents (staggered by 1h) ===
0 0,6,12,18 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 1,7,13,19 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 3,9,15,21 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python agent.py >> logs/agent.log 2>&1

# === ATProto Publisher (15 min after the last domain agent, now Fire) ===
15 3,9,15,21 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/ATProto && .venv/bin/python publisher.py >> logs/publisher.log 2>&1
```

The Synthesis agent does **not** run via either of the above — it's an Azure
Container Apps Job (`0 6,18 * * *` UTC), deployed via `Synthesis/deploy/deploy.sh`.
See `CONTEXT.md` for the deployment details.

---

## MCP Servers

Each domain stack exposes its data as MCP tools. The MCP servers run as
stdio subprocesses spawned by the agent — not as persistent services.
This keeps the architecture simple while preserving the MCP tool contract.

To inspect tools interactively:
```bash
python mcp_server.py --http --port 8000
# Open MCP Inspector → connect to http://localhost:8000/mcp
```

HTTP port assignments: River=8000, Weather=8001, AQI=8002, Fire=8003.

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

## ATProto publisher — the real message bus

Domain agents publish observations as structured lexicon records to the node's
own self-hosted PDS using a custom lexicon:

```
net.cpricedomain.temp.monitor.observation
```

This is not a posting mechanism — it's how the Synthesis agent receives domain
observations in the distributed design. Fields include both the domain agent's
prose summary and numeric readings (temperature, discharge, gage height, AQI
values, etc.) joined from the collector DBs at publish time. ATProto records
are DAG-CBOR, which has no float type, so numeric fields are stringified —
worth knowing if you extend the lexicon further.

`ATProto/publisher.py` runs per node (not per domain stack) and:
1. Reads `agent_observations` from each domain's local SQLite DB
2. Joins numeric fields from the corresponding collector table
3. Publishes new records to the node's PDS as `net.cpricedomain.temp.monitor.observation`
4. Uses the node's own PDS account as the author identity — no accompanying
   `app.bsky.feed.post`, since domain agents have no human audience

A Postman collection covering every ATProto XRPC call this project makes —
grouped by which file makes it, with real request/response shapes — is at
`postman/`. Useful for seeing the raw HTTP underneath the Python without
reading the code.

---

## Distributed identity

Each node (Pi running domain agents) has its own ATProto DID, minted by its
own self-hosted PDS (`napa-node-01.watershed-agent.dev`, reachable via a
Cloudflare Tunnel — see `ATProto/pds/README.md`) rather than borrowed from a
Bluesky account signup. The Synthesis agent — running separately, in Azure —
fetches from that PDS via `com.atproto.repo.listRecords` and filters for
`net.cpricedomain.temp.monitor.observation` records.

Before reasoning on any record, Synthesis verifies the author DID against
`Synthesis/publishers.json`, a trusted-publisher registry. This is the
workload identity boundary: a record from an unrecognised DID is discarded,
not reasoned on.

This surfaces interesting identity questions that connect to Ping Identity's work:
- How does a node prove it's an authorised publisher?
- How is the trusted DID registry maintained and updated? (Currently a hand-edited
  JSON file — see `CONTEXT.md`'s "Agent Identity Registry" discussion for where
  this is headed.)
- What happens when a node's DID is revoked mid-run?
- Can a compromised node publish plausible-looking records that fool Synthesis?

The MCP servers already support HTTP mode (`--http` flag) for when domain agent
tools need to be called across the network. Candidate workload identity approaches
for that boundary: SPIFFE/SVID per node, charter-based authorisation, PKI/X.509
certificates — see `CONTEXT.md` for where this is actively being worked out.

---

## Author

Chris Price — [@cpricedomain.net](https://bsky.app/profile/cpricedomain.net)  
Distinguished Sales Engineer, Ping Identity  
Napa, California
