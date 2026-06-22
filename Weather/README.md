# Weather Agent

Autonomous Napa County weather monitoring agent. Fire weather and precipitation focus.

## Architecture

Identical pattern to the watershed stack:

```
[NWS API] → [collector.py] → [SQLite: weather.db]
                                      ↓
                            [mcp_server.py] ← MCP tools
                                      ↓
                            [agent.py] → Claude (Haiku)
                                      ↓
                            [agent_observations table]
```

## Data sources

- **Observations**: Napa County Airport (KAPC) — hourly automated readings
- **Alerts**: NWS zones CAZ505 (Napa interior valleys, fire weather) and CAC055 (Napa County general)
- No API key required — NWS is public, requires only a User-Agent header

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install anthropic httpx "mcp>=1.27,<2"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
python collector/collector.py --init
python collector/collector.py
python agent/agent.py --dry-run --verbose
python agent/agent.py
```

## Cron

```cron
# Collect every 30 minutes (NWS updates hourly, no point going faster)
*/30 * * * * cd /home/cprice/Agentic/Weather && .venv/bin/python collector/collector.py >> logs/collector.log 2>&1

# Agent reasons every 6 hours
0 */6 * * * cd /home/cprice/Agentic/Weather && ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
```

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_current_conditions()` | Latest reading + 7-day stats |
| `get_recent_observations(n)` | Last N hourly readings |
| `get_observations_since(hours_ago)` | Time-windowed readings |
| `get_active_alerts()` | Live NWS watches/warnings |
| `get_fire_risk_indicators()` | Composite fire weather view |
| `get_recent_agent_observations(n)` | Agent memory |
| `write_agent_observation(...)` | Agent writes conclusion |

## Fire risk thresholds (in agent prompt)

- Active Red Flag Warning or Fire Weather Watch
- Temp ≥ 90°F AND humidity ≤ 25% AND wind ≥ 15 mph
- Humidity ≤ 15% (regardless of other factors)
- Wind gusts ≥ 45 mph
- NE/E wind direction = Diablo wind pattern (agent knows this)

## Next: Synthesis Agent

The synthesis agent reads `agent_observations` from both this stack
and the watershed stack to produce cross-domain risk assessments.
Low river flow + high temp + low humidity + NE winds = elevated fire risk corridor.
