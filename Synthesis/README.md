# Synthesis Agent

Cross-domain environmental risk assessment for Napa Valley.
Reasons across observations from all three domain agents to produce unified risk assessments.

## Distributed architecture intent

The Synthesis agent is designed to run on a **separate machine** from the node agents.
In the target architecture:

```
ATProto firehose
       ↓
[Synthesis agent]  ← filters by com.napavalley.monitor.observation lexicon
       ↓             verifies publisher DIDs against trusted registry
[synthesis.db]
       ↓
(Bluesky post when flagged)
```

Domain agents on the Pi publish observations to ATProto as structured records.
Synthesis subscribes to the firehose, filters by the custom lexicon, and verifies
that each record's author DID is in the trusted registry before reasoning on it.

## Current deployment (prototype)

```
[Watershed agent_observations] ──┐
[Weather agent_observations]   ──┼──→ [Synthesis agent] → [synthesis.db]
[AQI agent_observations]       ──┘
```

Reads SQLite directly from domain databases on the same Pi.
This is a working stand-in until the ATProto publisher and firehose subscriber are built.

No collector. No MCP server.

## Key difference from domain agents

Domain agents ask: "What is happening in my domain?"
The synthesis agent asks: "What does the combination mean?"

The interesting signal is in intersections:
- High temp + low humidity + NE winds + PM2.5 rising = fire risk corridor
- Rising river + active precipitation + flood watch = act now
- PM2.5 spike alone = smoke somewhere upwind, monitor wind direction

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install anthropic

export ANTHROPIC_API_KEY=sk-ant-...
```

Note: only `anthropic` needed — no MCP, no httpx. Reads SQLite directly.

## Usage

```bash
# First run (domain agents may not have data yet — that's fine)
python agent/agent.py --dry-run --verbose

# Live run
python agent/agent.py

# Use Sonnet for richer cross-domain reasoning (default)
python agent/agent.py --model sonnet

# Read more domain history per run
python agent/agent.py --observations 10
```

## Cron

```cron
# Synthesis runs twice daily — no need to run every 6 hours
# Offset from domain agents (which run at :00, :01, :02) — run at :03
0 6,18 * * * cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
```

## Full cron schedule — all four stacks

```
Watershed collector:  */15 * * * *
Weather collector:    */30 * * * *
AQI collector:        */30 * * * *

Watershed agent:      0 0,6,12,18 * * *
Weather agent:        0 1,7,13,19 * * *
AQI agent:            0 2,8,14,20 * * *

Synthesis agent:      0 6,18 * * *       ← reads after morning/evening domain runs
```

## Output schema

Each synthesis observation stores:
- `summary`          — plain language, suitable for Bluesky post
- `fire_risk`        — none/low/moderate/high/extreme
- `flood_risk`       — none/low/moderate/high/extreme
- `air_quality_risk` — none/low/moderate/high/extreme
- `overall_risk`     — none/low/moderate/high/extreme
- `flagged`          — true if overall_risk ≥ moderate or any domain = high/extreme
- `flag_reason`      — brief reason if flagged
- `reasoning`        — full cross-domain reasoning

## Model choice

Synthesis defaults to Sonnet (not Haiku) because cross-domain reasoning
across 5 domain observations each is more complex than single-domain assessment.
Use `--model opus` for the most thorough analysis.

## Next: Bluesky publisher

When flagged=true, post summary to ATProto.
The summary field is already written for a public audience.
```
