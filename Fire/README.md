# Fire Agent

Autonomous satellite fire-hotspot detection agent for Napa Valley. No
conversation, no human in the loop.

## Why this domain exists

Weather already covers fire *weather* (wind, humidity, Red Flag Warnings).
AQI already covers the *smoke signature* (PM2.5 rising with a flat ozone
fingerprint, consistent with wildfire smoke transport). Neither can answer
the question Synthesis's own cross-domain reasoning has repeatedly flagged
as an open uncertainty: **is there an actual fire out there**. This domain
answers that specifically, via satellite thermal detection — not weather
conditions favorable for fire, not smoke inferred from air quality, but a
real detected heat source.

## Architecture

```
[NASA FIRMS API] → [collector.py] → [SQLite: fire.db]
                                            ↓
                                  [mcp_server.py] ← MCP tools
                                            ↓
                                  [agent.py] → Claude (Haiku)
                                            ↓
                                  [agent_observations table]
                                            ↑
                                  (next run reads this as memory)
```

## Data source

[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) (Fire Information for
Resource Management System) — near-real-time satellite hotspot detections
(VIIRS/MODIS). Free, no cost, requires a free `MAP_KEY`:

1. Register at https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Set `FIRMS_API_KEY` in your environment (same var name pattern as
   `AIRNOW_API_KEY` for the AQI stack)

Bounding box, satellite source, and day-range are configured in
`node_config.json`'s `"fire"` block — not hardcoded here, same pattern as
every other domain's location config.

## Setup

```bash
pip install anthropic httpx "mcp>=1.27,<2"
export ANTHROPIC_API_KEY=sk-ant-...
export FIRMS_API_KEY=your-firms-map-key
```

## Usage

**Step 1 — Initialise the database:**
```bash
python collector.py --init
```

**Step 2 — Collect hotspot data:**
```bash
python collector.py            # single poll
python collector.py --loop     # poll every 30 minutes
```

**Step 3 — Run the agent:**
```bash
python agent.py                # single run, Haiku
python agent.py --dry-run      # reason but don't write observation
python agent.py --verbose      # print full Claude response
```

## What gets published

`ATProto/publisher.py` joins the nearest hotspot's distance/confidence/FRP
from `fire.db` into the published lexicon record's `fire` block (see
`ATProto/publisher.py`'s `build_fire_record()`), alongside the agent's
`summary`/`flagged`/`reasoning` — same pattern as the other three domains.

## Known limitations

- FIRMS NRT data itself typically updates a few times per day, not
  continuously — the collector polling every 30 minutes doesn't mean new
  satellite passes every 30 minutes, just that we check that often.
- Confidence field format differs by source: VIIRS uses `l`/`n`/`h`
  (low/nominal/high), MODIS uses a 0-100 numeric scale. The agent's system
  prompt doesn't currently normalize between the two — worth revisiting if
  `node_config.json`'s `fire.source` is ever changed from the VIIRS default.
- This domain has no equivalent of "named incident" data (e.g. official
  CAL FIRE incident names) — deliberately deferred, see `CONTEXT.md` for
  why (a named-incident feed is a second, less reliably-documented public
  API, and spatially matching it against hotspots is a real second piece
  of logic best added once this simpler version is proven).
