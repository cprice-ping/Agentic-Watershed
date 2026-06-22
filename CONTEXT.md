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

### Distributed identity exploration (follows ATProto work)
- Move Synthesis agent to a separate device once firehose subscriber is built
- Explore how node DIDs are provisioned, rotated, and revoked
- What happens when Synthesis receives a record from a revoked DID mid-run?
- This connects directly to Ping Identity's workload identity work

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
