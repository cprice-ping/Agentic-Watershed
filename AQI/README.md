# AQI Agent

Autonomous Napa County air quality monitoring agent.
Primary purpose: wildfire smoke early detection via PM2.5 spike analysis.

## Architecture

Same pattern as Watershed and Weather stacks:

```
[AirNow API] → [collector.py] → [SQLite: aqi.db]
                                        ↓
                              [mcp_server.py] ← MCP tools
                                        ↓
                              [agent.py] → Claude (Haiku)
                                        ↓
                              [agent_observations table]
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install anthropic httpx "mcp>=1.27,<2"

export ANTHROPIC_API_KEY=sk-ant-...
export AIRNOW_API_KEY=your-airnow-key   # free: https://docs.airnowapi.org/login
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
# AQI collector every 30 minutes (AirNow updates hourly)
*/30 * * * * cd /home/cprice/Agentic/AQI && AIRNOW_API_KEY=... .venv/bin/python collector/collector.py >> logs/collector.log 2>&1

# AQI agent every 6 hours (offset 2h from watershed, 1h from weather)
0 2,8,14,20 * * * cd /home/cprice/Agentic/AQI && ANTHROPIC_API_KEY=sk-ant-... AIRNOW_API_KEY=... .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
```

## Full cron schedule across all three stacks

```
Watershed collector:  */15 * * * *          (every 15 min)
Weather collector:    */30 * * * *          (every 30 min)
AQI collector:        */30 * * * *          (every 30 min)

Watershed agent:      0 0,6,12,18 * * *    (midnight, 6am, noon, 6pm)
Weather agent:        0 1,7,13,19 * * *    (1am, 7am, 1pm, 7pm)
AQI agent:            0 2,8,14,20 * * *    (2am, 8am, 2pm, 8pm)
```

Agents are staggered by 1 hour so they never run simultaneously.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_current_aqi()` | Latest PM2.5 and Ozone readings |
| `get_aqi_since(hours_ago)` | Time-windowed readings |
| `get_aqi_trend(days)` | Daily min/mean/max trend |
| `get_smoke_indicators()` | PM2.5 spike detection, rate-of-change |
| `get_recent_agent_observations(n)` | Agent memory |
| `write_agent_observation(...)` | Agent writes conclusion |

## Key insight: PM2.5 as fire early warning

A PM2.5 AQI spike without a corresponding Ozone rise = likely wildfire smoke.
The AQI agent explicitly looks for this pattern. Combined with the weather
agent's wind direction data (NE/E = Diablo winds = offshore flow pushing
smoke into the valley), this becomes a meaningful early warning signal.

## Next: Synthesis Agent

Reads agent_observations from all three databases.
Cross-domain risk: low river flow + high temp + low humidity + PM2.5 rising = act.
```
