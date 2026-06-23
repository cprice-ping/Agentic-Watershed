# Watershed Agent

Autonomous Napa River monitoring agent. No conversation, no human in the loop.

## Architecture

```
[USGS API] → [collector.py] → [SQLite: watershed.db]
                                        ↓
                              [mcp_server.py] ← MCP tools
                                        ↓
                              [agent.py] → Claude (Haiku)
                                        ↓
                              [agent_observations table]
                                        ↑
                              (next run reads this as memory)
```

## Setup

```bash
pip install anthropic httpx "mcp>=1.27,<2"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

**Step 1 — Initialise the database:**
```bash
python collector/collector.py --init
```

**Step 2 — Collect some data:**
```bash
python collector/collector.py          # single poll
python collector/collector.py --loop   # poll every 15 min (leave running)
```

**Step 3 — Run the agent:**
```bash
python agent/agent.py                  # single run with Haiku
python agent/agent.py --model sonnet   # richer reasoning
python agent/agent.py --dry-run        # reason without writing to DB
python agent/agent.py --verbose        # see full Claude response
```

**Step 4 — Test the MCP server directly (optional):**
```bash
python mcp_server/mcp_server.py --http   # runs on http://localhost:8000
# Then open MCP Inspector and connect to http://localhost:8000/mcp
```

## Cron setup (Pi)

```cron
# Collect every 15 minutes
*/15 * * * * cd /home/pi/watershed && python collector/collector.py >> logs/collector.log 2>&1

# Agent runs every 6 hours
0 */6 * * * cd /home/pi/watershed && python agent/agent.py >> logs/agent.log 2>&1
```

## USGS Stations

| Station ID | Location | Parameters |
|------------|----------|------------|
| 11458000 | Napa River near Napa | Discharge (cfs), Gage Height (ft) |
| 11456000 | Napa River near St Helena | Discharge (cfs), Gage Height (ft) |

NWS Flood Stage at 11458000: 25 ft

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_recent_readings(n)` | Last N readings, all stations |
| `get_readings_since(hours_ago)` | Time-windowed readings |
| `get_station_summary(station_id)` | Latest + 7-day stats per station |
| `get_anomalies(threshold_pct)` | Readings deviating from 30-day mean |
| `get_recent_observations(n)` | Agent's past conclusions (memory) |
| `write_agent_observation(...)` | Agent writes conclusion back to DB |

## What the agent learns

Each run the agent:
1. Reads its last 3 observations (memory/continuity)
2. Gets current station summaries with 7-day baselines
3. Scans for anomalies (>40% deviation from 30-day mean)
4. Reviews the last 48h of readings
5. Reasons with Claude and writes a structured observation

The `flagged` field on observations is the signal for future alerting
(Bluesky posts, notifications, etc).

## Next steps

- `bluesky_publisher.py` — post flagged observations to ATProto
- Custom lexicon: `net.cpricedomain.temp.monitor.observation`
- Add more stations (Conn Creek, Milliken Creek)
- Physical sensors via Pi GPIO → same collector interface
