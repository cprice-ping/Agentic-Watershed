# Project Context — Agentic Watershed

Living document. Update this as the project evolves so coding agents and
collaborators can pick up where things left off without needing the full
conversation history.

Last updated: 2026-07-02

---

## What this is

A distributed system of autonomous agents connected by identity and a federated protocol.
The domain is Napa Valley environmental data. That's the concrete surface — the actual
subject is the architecture:

- **Edge agents with workload identity** — each node runs on a Raspberry Pi, reasons
  locally with Claude Haiku, and publishes structured records to ATProto under its own
  DID. The DID is the agent's identity, not a login credential.
- **ATProto as message bus, not Bluesky as destination** — domain agents publish to
  their own self-hosted PDS (`napa-node-01.watershed-agent.dev`), reachable via a
  Cloudflare Tunnel — no dependency on `bsky.social` infrastructure for the node's
  identity or data. Only Synthesis touches the public Bluesky network, and only for
  the human-facing advisory. Records flow over a federated protocol using a custom
  lexicon (`net.cpricedomain.temp.monitor.observation`). Any agent that knows the
  lexicon and trusts the DID can participate, from anywhere.
- **DID-based trust boundary** — the Synthesis agent verifies publisher DIDs against
  a trusted registry (`publishers.json`) before acting on any record. Unrecognised
  nodes are rejected, not silently trusted.
- **Synthesis at the cloud layer** — a separate agent (Azure Container Apps Job)
  fetches from the node's PDS, reasons across domains with Claude Sonnet, resolves
  its own prediction ledger, and posts the human-facing advisory to Bluesky
  (`napasynth01.bsky.social`) — the only step in the pipeline a person ever sees.

The environmental monitoring domain is well-suited because it has real APIs, genuine
cross-domain reasoning, and seasonal patterns worth tracking over time. The architecture
pattern (edge agent → structured record → verified identity → synthesis) is the point.

---

## Lineage and kindred work

The seed was Ruthanna Emrys' novel **"A Half-Built Garden"** — a vision of
decentralised, federated systems operating at human (and non-human) scale without
a central authority, and of non-human participants treated as first-class actors in
a shared network. Watching what **AT Protocol** was actually building — portable
identity (`did:plc`), data bound to the identity rather than the platform, and
federation as a first principle — made it concrete: this is the closest existing
substrate for agents that own their identity and history independent of where they run.

Identity is a small but critical part of the bigger idea. The bigger idea is
autonomous agents as portable, verifiable, federated participants in a shared
information space — perceiving, reasoning, and publishing without central coordination
or a human in the loop. Identity is what makes the trust boundary possible, but it
serves the architecture, not the other way around.

Adjacent thinkers and projects working nearby ground:

- **Bluesky / ATProto** — user-owned identity and data, PDS portability. The substrate.
- **Spritely Institute** (Christine Lemmer-Webber, co-author of ActivityPub) —
  object capabilities and distributed identity at the protocol level; the strongest
  "no central authority" research thread.
- **DIF (Decentralised Identity Foundation)** — standards home for `did:web`, VCs,
  and the agent-identity problem. The specs this project builds against.
- **Ink & Switch** (Geoffrey Litt et al.) — agents as first-class collaborators in
  systems rather than tools; shares the "agent as peer" framing.
- **Ceramic, Transmute (Orie Steele)** — decentralised data and machine/non-human
  identity, adjacent but more blockchain-native than this project needs.

**The unoccupied space:** nobody is quite using ATProto as the message bus for
agent-to-agent communication with DID-based trust between autonomous nodes. Most
agent-identity work is either blockchain-native (heavy) or OAuth-native (human-first).
The combination here — ATProto portability + `did:web` simplicity + autonomous edge
agents — is relatively unexplored. That's the generative gap.

---

## Current deployment state

Running on a Raspberry Pi 5, Napa, California.
All stacks deployed under `/home/cprice/Agentic-Watershed/`.

### Collectors — all running via cron

| Stack | Frequency | Status |
|-------|-----------|--------|
| Watershed | every 15 min | ✅ Running, storing to `watershed.db` |
| Weather | every 30 min | ✅ Running, storing to `weather.db` |
| AQI | every 30 min | ✅ Running, storing to `aqi.db` |
| Fire | every 30 min | ✅ Added 2026-07-06, storing to `fire.db` |

### Domain agents — all running via cron

| Stack | Schedule | Status |
|-------|----------|--------|
| Watershed | 0,12h | ✅ Running, writing observations (cut from 0,6,12,18h 2026-07-20 — cost) |
| Weather | 1,13h | ✅ Running, writing observations (cut from 1,7,13,19h 2026-07-20 — cost) |
| AQI | 2,8,14,20h | ✅ Running, writing observations — kept at 4x/day, fastest-moving signal |
| Fire | 3,15h | ✅ Added 2026-07-06, writing observations (cut from 3,9,15,21h 2026-07-20 — cost) |

### Fire domain — NASA FIRMS satellite hotspot detection

Fourth domain, added 2026-07-06. Motivation: Synthesis's own cross-domain
reasoning had repeatedly flagged "the upwind fire source has not been
identified or confirmed extinguished" as an open uncertainty across
multiple real runs — Weather covers fire *weather*, AQI covers the *smoke
signature*, but nothing looked for an actual fire. This closes that gap
directly: [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) satellite
hotspot detections (VIIRS near-real-time), free public API, requires a
free `MAP_KEY` (env var `FIRMS_API_KEY`).

Deliberately does **not** attempt to attach named-incident data (e.g.
official CAL FIRE incident names) to detected hotspots. That was considered
and explicitly deferred — FIRMS is a clean, well-documented, versioned
public API on par with USGS/NWS/AirNow already in this project; a
named-incident feed (CAL FIRE or NIFC) is not documented to the same
standard and would need a second, less reliable data source plus a real
spatial-matching layer (nearest named incident within some distance of a
hotspot). Worth doing as a fast-follow once FIRMS itself is proven in
production, not as part of the first pass.

Same architecture pattern as every other domain: `Fire/collector.py` (FIRMS
Area API → `hotspots` table, deduped on lat/lon/acq_date/acq_time/satellite,
haversine distance from Napa center precomputed and stored) →
`Fire/mcp_server.py` (MCP tools, port 8003) → `Fire/agent.py` (Haiku, forced
tool-use from the start — built after the tool-use fix, so it never had the
free-text JSON parsing bug the other four agents needed fixing). Wired into
`ATProto/publisher.py` (`build_fire_record()`, `_fetch_fire_numerics()` —
nearest hotspot's distance/confidence/FRP plus a 6h hotspot count, same
DAG-CBOR string-not-float handling as the other domains) and into
Synthesis's reasoning (`agent_atproto.py`'s domain filter and system prompt
both updated — see "FIRE DETECTION" section of the system prompt for the
specific guidance: an empty hotspot list means "nothing detected in the
monitored bounding box," not "no fire," since a fire could be upwind but
outside the bbox or not yet caught by a satellite pass).

Bounding box (`node_config.json`'s `"fire"` block, not hardcoded):
`-123.3,37.7,-121.8,39.0`, roughly Napa/Sonoma/Solano/Lake counties — sized
for regional smoke-transport awareness, not just fires within county lines.

**Multi-source polling (2026-07-15).** Originally polled `VIIRS_SNPP_NRT`
only. Caught a real gap during an active Watch Duty-tracked fire: SNPP
reported zero hotspots in the bbox for the better part of a week while
`VIIRS_NOAA20_NRT` and `VIIRS_NOAA21_NRT` both had current detections the
whole time, including one 18mi from Napa center with FRP climbing from 5.9
to 23.0 MW between passes — inside the alert threshold, missed entirely.
Each VIIRS satellite has its own ~12h-offset overpass schedule; querying
one is querying a third of the available coverage. `fire.source` (string)
is now `fire.sources` (list) in `node_config.json`, defaulting to all
three platforms. `collector.py` polls each and merges — the existing
dedup key already includes `satellite`, so this needed no schema change,
just a loop. Confirmed live the same day: the missed hotspot showed up on
the next manual collector run and the Fire agent flagged it correctly.

Also that week: `get_recent_observations` in River and Fire was reading
back the full `reasoning` column as memory instead of just `summary` —
`write_agent_observation`'s own docstring already says summary is what
future runs should see, so this was drift, not a design choice. Fixed
after the daily token-cost graph showed Haiku (the domain agents) costing
more than Sonnet (Synthesis) by 4-5x, which is backwards from what the
per-token pricing would suggest and pointed straight at prompt size.

### Self-hosted PDS — node identity, off Bluesky infrastructure

| Component | Status |
|-----------|--------|
| PDS (official `bluesky-social/pds`, Docker on the Pi) | ✅ Running, `napa-node-01.watershed-agent.dev` |
| Cloudflare Tunnel (`cloudflared`, systemd service) | ✅ Live — no inbound ports opened on the Pi's router |
| Domain: `watershed-agent.dev` | Registered + DNS-hosted directly via Cloudflare Registrar |
| Node DID | `did:plc:ggztd5hjk3cnkhgzdk4rmqan` (replaces the old `bsky.social`-issued one) |

Domain agents publish structured lexicon records to this PDS, not to Bluesky —
`ATProto/publisher.py` no longer sends an accompanying `app.bsky.feed.post`. First
confirmed end-to-end publish 2026-07-01. See `ATProto/pds/README.md` for the full
setup (DNS delegation didn't work at the registrar level — see "Architecture
decisions" below for why a dedicated domain was registered instead).

### Synthesis agent

| Schedule | Status |
|----------|--------|
| 6h, 18h UTC | ✅ Running in Azure Container Apps Job (`synthesis-agent`, `rg-agentic-watershed`, `westus2`) |

The Synthesis agent runs in Azure, not on the laptop. Pi (edge) → self-hosted PDS →
Azure (cloud) → Bluesky advisory. Redeployed 2026-07-02 with `publishers.json`
updated to the new DID — confirmed live: subscriber fetched 8 records from
`napa-node-01.watershed-agent.dev`, resolved 2 pending predictions against real
data, reasoned across domains, posted the advisory to `napasynth01.bsky.social`.

That first redeploy used a single global `ATPROTO_PDS_URL` for all fetches —
fine with one node, but silently wrong once a second node runs its own separate
PDS (it would only ever query the configured URL, missing the other node's
records with no error). `subscriber.py` now resolves each trusted publisher's
PDS individually via `plc.directory`, same pattern as `Viewer/index.html`'s
`resolvePds()` — see "DID resolution" below. `ATPROTO_PDS_URL` is no longer
read by the subscriber or set on the Azure job.

Since 2026-09-06 the image rebuilds and redeploys from GitHub Actions on any
merge to main touching `Synthesis/`, authenticated by OIDC rather than a stored
credential. Images carry the commit SHA, and the workflow reads the job back
and fails if the running image isn't the commit that just built — `:latest`
alone couldn't distinguish "redeployed" from "unchanged", which is how the
question "is Azure running current code?" kept coming up. `deploy.sh
--image-only` still works by hand; DEPLOYMENT.md has the one-time Azure setup.

Accumulating domain observations — first meaningful cross-domain synthesis
expected after 2-3 days of data. Baseline established on first run (2026-06-22):
low fire risk, no flood risk, low AQI risk. Marine influence dominant.

### Containerization — built, node-01 migration pending

`docker-compose.yml` (repo root) containerizes the collectors, domain agents,
and ATProto publisher — everything except the PDS, which already had its own
compose file (`ATProto/pds/`). Two images: a shared one for River/Weather/AQI
(identical deps), a lighter one for the publisher (`httpx` only, no
`anthropic`/`mcp`). `node_config.json` and `.env` are bind-mounted rather than
baked in, so the built images are node-agnostic — deploying node-02 is
"clone, write new config, done," not "rebuild an image with different
hardcoding." Cron lines change from `.venv/bin/python script.py` to
`docker compose run --rm <service> python script.py` — same one-shot,
run-to-completion shape as Synthesis's Azure Container Apps Job, applied
node-side. See `DEPLOYMENT.md` for fresh-node setup and, more carefully, the
migration path off node-01's current venv+cron setup without losing its
existing SQLite history (the compose bind-mounts point at the same
`<Stack>/data/` paths the venv setup already writes to — no export/import,
just point cron at the new invocation once a manual test run confirms it
works).

Not yet cut over on node-01 — this exists as tested-but-unswapped capability,
same posture as anything else in this doc marked "built, not yet live."

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
- `FIRMS_API_KEY` — used by Fire collector (NASA FIRMS `MAP_KEY`, free registration
  at https://firms.modaps.eosdis.nasa.gov/api/map_key/)
- `BSKY_HANDLE` / `BSKY_APP_PASSWORD` (Pi) — node's self-hosted PDS account
  credentials, e.g. `napa-node-01.watershed-agent.dev` — not a Bluesky app
  password despite the variable names (kept for continuity with the publisher's
  original bsky.social-based auth flow, which is unchanged, just pointed elsewhere)
- `ATPROTO_PDS_URL` (Pi only) — which PDS `ATProto/publisher.py` publishes to.
  Defaults to the self-hosted PDS; falls back to `bsky.social` if unset. Not
  used on Azure/by the subscriber — see "Synthesis agent" above and "DID
  resolution" below for why fetch-side PDS lookup is per-DID, not a single URL.

---

## Known issues / notes

- AirNow API occasionally returns empty responses — collector logs a warning and retries next poll. Normal behaviour, not a bug.
- USGS qualifiers are returned as plain strings not dicts — fixed in collector.py (parse_usgs_response).
- Weather and AQI venvs needed to be created separately from Watershed — each stack is fully independent.
- `/etc/environment` is not loaded automatically by cron — sourced explicitly with `. /etc/environment &&` prefix on each cron line.
- **ATProto records are DAG-CBOR — no float type exists in the data model.**
  Only `null, boolean, integer, string, cid, bytes, array, object` are valid.
  Any numeric field pulled from a SQLite `REAL` column must be stringified before
  going into a record, or `createRecord` rejects it with `InvalidRequest`. Fixed
  in `publisher.py` via `_atproto_safe()`; worth remembering for any future field.
- Watershed records published one gauge's reading as the whole watershed's,
  2026-09-06 — fixed. The node polls two USGS stations, Napa (11458000) and
  St Helena (11456000), and both report the same parameter codes. The
  publisher's query selected parameter code and value with no station column
  and kept whichever row sat nearest in time, so one station won both fields
  and the other was dropped without trace. The record that surfaced it claimed
  0.0 cfs and 0.47 ft while its own summary read "Near Napa: 0.14 cfs at
  2.09 ft" — St Helena, which is dry in September, standing in for a river
  that was still flowing. The lexicon had declared the right shape from the
  start (`dischargeMinCfs`/`MeanCfs`/`MaxCfs`, `gageHeightMinFt`/`MaxFt`) and
  the publisher had never implemented it, emitting undeclared singular fields
  instead. Same family as the Fire distance bug: the numeric block and the
  prose summary were computed from different data and nothing compared them.
- `sevenDayTrend` was hardcoded to "unknown" on every watershed record ever
  published, while the agent's own summary computed the trend in prose. Now
  derived from discharge, comparing the older half of the seven-day window
  against the newer half over stations present in both — a gauge dropping out
  mid-window would otherwise move the aggregate on its own and read as a
  trend in the river.
- Weather records published field names the lexicon never declared, 2026-09-07
  — fixed. The publisher wrote the collector's raw SQLite column names
  (`temperature_f`, `wind_gust_mph`, `precip_24h_mm`) while `weatherData`
  declares `temperatureF`, `windGustMph`, `precipMm24h`. Anything reading the
  lexicon and looking for the declared names found nothing. Synthesis had
  already worked around it with a `_num()` helper accepting either spelling,
  which is why it went unnoticed for months — the workaround made the record
  wrong in a way nothing complained about. Renamed at the fetch boundary; the
  old names stay readable on the consumer side because records carrying them
  are still inside the lookback window.
- `activeAlerts` was hardcoded to `[]` on every weather record ever published,
  which is not "no alerts known" but an assertion that none were active. The
  collector has been storing NWS alerts in its `alerts` table the whole time,
  and Synthesis has a `FIRE_CONFIRM_ALERTS` branch that could never fire.
  Alerts are now filtered by their own onset/expires against observedAt rather
  than by collection time, and parsed as datetimes rather than compared as
  strings — NWS returns local offsets, so `2026-09-06T20:00:00-07:00` sorts
  wrongly against a UTC anchor.
- `windPattern` is now derived by the publisher from direction and speed, which
  is a lookup rather than a judgment. It deliberately never returns "valley"
  even though the lexicon lists it: Napa Valley runs NNW-SSE, so up-valley flow
  arrives from the same southerly sector as marine air and direction alone
  cannot separate them. Sectors outside the Diablo and marine arcs return
  "unknown" and the field is omitted. Same reasoning as leaving `fireRisk`
  absent — a category that might be wrong is worth less than no category.
- Flag thresholds were written out four times per domain and had drifted,
  2026-09-08 — consolidated into `<Domain>/thresholds.py`. The worst case was
  Weather: `get_fire_risk_indicators` handed the model a `fire_risk_thresholds`
  block reading 20% humidity, 20 mph wind and 35 mph "critical wind", while
  `agent.py`'s prompt, `flag_rules.py` and Synthesis all used 25%, 15 mph and
  45 mph gusts. Three of four numbers disagreed, and the wrong copy arrived
  inside the same JSON as the measurements, so it read as fact rather than as
  instruction. That is the source of the recurring "well below critical 15%
  and 20% thresholds" line in published summaries — the model was quoting both
  rulebooks because it had been given both.
  The prompt criteria are now generated from the constants via
  `string.Template` (not `.format()` — the prompts contain literal JSON braces),
  the tool returns `thresholds.as_dict()`, and `flag_rules.py` imports the same
  names. Changing a number moves all three in one edit. River has no numeric
  flag criteria at all, which is why it has no `flag_rules.py` and needs no
  thresholds module.
  `ATProto/publisher.py` loads `Fire/thresholds.py` by file path rather than
  import, because it ships in its own image and four modules named `thresholds`
  would collide on `sys.path`. A missing module raises rather than defaulting:
  a silent fallback to a locally-guessed window is precisely the divergence
  that published a five-day-old hotspot as an 8.5-mile threat.
  Synthesis's `FIRE_WX_*` constants are deliberately left as a fifth copy. They
  decide whether a prediction is confirmed, so importing the node's numbers
  would let the node define what counts as confirmation of the node's own flag
  — the same mirror that made the old ledger read 128 confirmed and 0 expired.
  The Synthesis image cannot see the domain code anyway, which enforces it.
- `ATProto/Dockerfile` never copied `node_config.json`, so that image crashed
  on import; `publisher.py` reads it from BASE at module level. Only cron on
  the Pi, running from a full checkout, was unaffected — which is why nobody
  noticed. Fixed alongside the thresholds copy the same Dockerfile now needs.
- NWS QuantitativeValue objects carry a `unitCode`, and it is not what you
  might assume: wind on the observations endpoint is `wmoUnit:km_h-1`, not
  m/s. `extract_value()` ignored it entirely and the collector assumed m/s,
  inflating every wind reading by 3.6x for the life of the project. Fixed
  2026-09-08 by converting from the declared unit; an unrecognised unitCode
  now drops the reading with a warning rather than guessing, because a missing
  reading is recoverable and a plausible-looking wrong one is not.
  Historical rows are corrected in place by an idempotent migration in
  `init_db`, guarded by a new `schema_migrations` table — the error is a known
  constant, so the correction is exact, and leaving it would have kept the
  48-hour trend window mixing real and inflated numbers for two days after
  deploy. Running it twice would divide by 12.96, hence the guard.
  Not rewritten: agent observation summaries already written, and published
  ATProto records, both of which quote the inflated figures permanently.
- `PDS_HOSTNAME` alone does not authorize account handles under that domain on a
  self-hosted PDS — `PDS_SERVICE_HANDLE_DOMAINS` (suffix match, leading dot) is
  required too.
- Recent `bluesky-social/pds` images don't ship `pdsadmin` or `dist/scripts/
  create-account.js` inside the container — account creation is a plain
  `com.atproto.server.createAccount` XRPC call instead.
- `cloudflared`'s systemd service runs as root and doesn't see the invoking
  user's `~/.cloudflared` — config and credentials need to live under
  `/etc/cloudflared/`.
- DNSimple (and most registrars) can't delegate a single subdomain's NS records
  without moving the whole zone — Cloudflare Tunnel's cert-issuance flow needs a
  zone already on Cloudflare's nameservers. Registering a small dedicated domain
  directly through Cloudflare Registrar sidesteps this entirely (see below).
- **Local SLMs (Ollama) tried for the domain agents, 2026-07-16 — not viable yet,
  keep Haiku.** Motivated by the token-cost review showing Haiku (domain agents)
  costing more than Sonnet (Synthesis) by 4-5x — see the memory-readback fix
  above for the real driver of that. Tested on an RPi5 8GB: `qwen2.5:3b-instruct-q4_K_M`
  and `qwen3.5:4b` (Ollama's `format` JSON-schema constraint, not prompt-only
  "respond in JSON," and `think=False` — `think=True` bypasses the schema
  constraint entirely, writing the full answer as free text into a separate
  `thinking` field and leaving `response` empty). RAM and latency were fine
  (~2-3GB resident, 15-270s per run depending on model/context size — all well
  within the 6h cron cadence). Correctness wasn't:
  - `qwen2.5:3b` on real Fire data: called two hotspots "high-confidence" when
    neither was (VIIRS confidence was `n`/`l`, never `h`), and its own summary
    contradicted itself — "unchanged, no significant changes" followed two
    sentences later by "a recent detection... new hotspots."
  - `qwen3.5:4b` on the same Fire data (`think=False`) fixed both of those, but
    with `think=True` it flipped to `flagged: false` by misreading the flag
    rule's `within 20mi OR high-confidence within 50mi` as `within 20mi AND
    high-confidence` — would have suppressed a real alert.
  - `qwen3.5:4b` on real Weather data (`think=False`) was worse, not better,
    despite Weather's rules being simple numeric thresholds — flagged `true`
    against current conditions that met none of the actual criteria (temp
    60.8°F, humidity 72.2%, no active alerts), justifying it with a "historical
    peak" / "compound risk factors" rationale that isn't part of the system
    prompt's rules at all. Worse result on the domain expected to be the
    easiest case, which is the actual finding here — not "Fire is too hard,"
    but "the failure mode doesn't clearly correlate with task difficulty,"
    which is a worse signal for trusting it unsupervised on any domain.
  Conclusion: the schema-constrained plumbing works reliably; the reasoning
  quality is the blocker, and it's not obviously fixable with prompt tuning —
  Weather's failure was a fabricated rule, not an ambiguous one. Revisit with
  a materially larger local model (9B+) or a different model family, not more
  tuning of these two. Pilot scripts (`Fire/ollama_pilot_test.py`,
  `Weather/ollama_pilot_test.py`) are throwaway, not wired into any agent.

---

## Cron (current)

```cron
# === Collectors ===
*/15 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python collector.py >> logs/collector.log 2>&1
*/30 * * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python collector.py >> logs/collector.log 2>&1
50 2,14 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python incidents_collector.py >> logs/incidents.log 2>&1

# === Domain Agents (River/Weather/Fire 2x/day, AQI 4x/day) ===
0 0,12 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 1,13 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 2,8,14,20 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/AQI && .venv/bin/python agent.py >> logs/agent.log 2>&1
0 3,15 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python agent.py >> logs/agent.log 2>&1

# === ATProto Publisher ===
15 3,9,15,21 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/ATProto && .venv/bin/python publisher.py >> logs/publisher.log 2>&1
```

River/Weather/Fire dropped from 4x/day to 2x/day on 2026-07-20 — Haiku's
aggregate cost across 16 agent runs/day was the dominant driver in the daily
token-cost review, well ahead of Synthesis on Sonnet at 2x/day (see the
memory-readback fix above for the other half of that cost story). AQI stays
at 4x/day since PM2.5 is the fastest-moving signal in the system. The
publisher's `3,9,15,21` schedule didn't need to change — it was always sized
around AQI's cadence, not a symmetric one-slot-per-domain design, so it still
picks up every agent's output within at most ~3h15m (worked out by hand
against the new schedule, not just assumed).

Synthesis runs as an Azure Container Apps Job (`0 6,18 * * *` UTC), not a Pi cron
entry — see "Synthesis agent" above.

---

## What's next

### Done
- [x] ATProto publisher — domain observations published as structured lexicon records
- [x] Synthesis subscriber — fetch-mode (cron-shaped, not firehose daemon), lookback window
- [x] Synthesis publisher — separate identity (`napasynth01.bsky.social`), advisory framing
- [x] End-to-end pipeline confirmed: Pi nodes → ATProto → Synthesis agent → advisory post
- [x] TRUSTED_PUBLISHERS / publishers.json in place as interim trust boundary (did:web registry replaces this)
- [x] Synthesis agent containerised and deployed to Azure Container Apps Job
- [x] Self-hosted PDS on the Pi (`napa-node-01.watershed-agent.dev`), fronted by a
      Cloudflare Tunnel — domain agents no longer depend on `bsky.social` for
      their own identity or data (2026-07-02)
- [x] Numeric fields (temperature, discharge, gage height, PM2.5/ozone AQI, etc.)
      now populated in lexicon records, joined from collector DBs at publish time
      (2026-07-02) — previously only `summary`/`flagged` were written
- [x] Trend-analysis field names in `agent_atproto.py` fixed to match what the
      publisher actually emits — was silently finding nothing (2026-07-02)
- [x] Containerization (docker-compose) for the domain stacks — built, node-01
      migration not yet cut over (2026-07-03)
- [x] Fourth domain added: Fire (NASA FIRMS satellite hotspot detection) —
      closes the "unidentified upwind fire source" gap Synthesis's own
      reasoning had repeatedly flagged (2026-07-06)
- [x] Fire collector polls all three VIIRS satellites instead of one —
      SNPP-only was missing real detections during a live event (2026-07-15)
- [x] River/Fire memory readback trimmed to summary only, not full reasoning
      text — was a real driver of Haiku token cost (2026-07-15)
- [x] Publisher no longer aborts the whole run when one domain raises — Fire
      being last in the list was the only reason the `fire.source` KeyError
      cost just fire and not weather and aqi too. Still exits non-zero, so a
      broken run doesn't read as a silent success to cron (2026-08-26)
- [x] Publisher logs the checkout's branch and commit at startup. The Fire
      crash sat fixed in main for four weeks while the Pi ran older code, and
      `git pull` reported "Already up to date" because the checkout wasn't on
      main. Nothing in publisher.log said which code produced a run
      (2026-08-26)
- [x] `subscriber.py` filters records outside the lookback window instead of
      returning on the first one. The early exit assumed listRecords' rkey
      order matched observedAt order, which stops being true after a backfill:
      republishing 46 stranded Fire records put four-week-old observations at
      the front of the repo, and the pre-fix subscriber fetched zero records
      as a result (2026-08-27)
- [x] `agentModel` records the model that actually ran. It had been a
      hardcoded string in the record builders, so `--model sonnet` published a
      record claiming Haiku. The value travels by environment variable rather
      than as a tool argument — it ends up in the field consumers use to weigh
      an observation, so the model must not be able to assert it about itself
      (2026-09-04)
- [x] Flag criteria evaluated in code alongside the model, shadow only.
      `<Domain>/flag_rules.py` for Fire, Weather and AQI; the verdict is stored
      next to the model's own and changes no behaviour. Making it authoritative
      waits on the divergence data, mostly because Fire's persistence exception
      is a de-escalation the rules deliberately don't implement (2026-09-04)
- [x] Token usage recorded per run, and Synthesis moved from Sonnet 4.6 to
      Sonnet 5 — a third cheaper on a newer model. `token_report.py` prices the
      recorded usage per domain. The split between domain agents and Synthesis
      had never actually been measured; the only prior data point was the
      "Haiku 4-5x Sonnet" observation, which turned out to be prompt size
      (2026-09-04)
- [x] Viewer shows the raw record behind each card. `agentModel` had been
      published on every record since #48 and was still unreachable from the
      UI without hand-building an XRPC call (2026-09-05)
- [x] Synthesis records comply with their own lexicon. Validating a live
      advisory found four violations nothing had complained about — summary
      and flagReason both over their maxLength, `reasoning` and `synthesisDid`
      undeclared. Neither PDS validates an unknown custom lexicon, so an
      invalid record publishes silently (2026-09-05)
- [x] Synthesis auto-deploys from GitHub Actions on merge to main, OIDC auth,
      images tagged by commit SHA. The deploy reads the job back afterwards and
      fails unless the running image is the commit that just built, so a green
      tick means the image landed rather than that the commands exited zero
      (2026-09-06)
- [x] Predictions resolve against measured conditions instead of another
      agent's `flagged` boolean — see "The prediction ledger was a mirror"
      below (2026-09-06)

### Host Synthesis agent outside the laptop — DONE

The Synthesis agent is now containerised and deployable to **Azure Container Apps Jobs**,
replacing the laptop cron with a cloud-hosted, cron-scheduled run-to-completion job.

**What was built:**

| File | Purpose |
|------|---------|
| `Synthesis/requirements.txt` | Python dependencies (anthropic, httpx, atproto) |
| `Synthesis/Dockerfile` | Container image — copies subscriber, agent, publisher |
| `Synthesis/entrypoint.sh` | Pipeline script: subscriber → agent_atproto → publisher |
| `Synthesis/deploy/deploy.sh` | Azure CLI provisioning script (full infrastructure + image) |
| `Synthesis/deploy/job.yaml` | Container Apps Job spec template (image + volume mount) |

**Architecture:**
- Image built via `az acr build` — no local Docker daemon required
- SQLite databases persisted on an **Azure File Share** mounted at `/data`
  (subscriber.db, synthesis.db, synth_publisher.db)
- Secrets (`ANTHROPIC_API_KEY`, `BSKY_SYNTH_HANDLE`, `BSKY_SYNTH_APP_PASSWORD`)
  injected as Container Apps secrets — never stored in the image or YAML
- Managed identity granted `AcrPull` on the registry — no credential rotation needed
- Schedule: `0 6,18 * * *` UTC (matching existing twice-daily cadence)
- Pipeline runs to completion in under 10 min; job is killed after 600s if hung

**To deploy:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export BSKY_SYNTH_HANDLE=napasynth01.bsky.social
export BSKY_SYNTH_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
cd Synthesis/deploy && ./deploy.sh
```

**To trigger a manual run:**
```bash
az containerapp job start --name synthesis-agent --resource-group rg-agentic-watershed
```

This is also the first non-local execution of a registry-aware agent once the
did:web registry is ready — the Synthesis DID will be provisioned against the
cloud instance, not a developer laptop.

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

**Running our own PDS — DONE (2026-07-02).** Removed the dependency on `bsky.social`
as host while keeping full ATProto compatibility and DID portability. See "Current
deployment state" above and the "Architecture decisions" section below for why.

### DID onboarding problem — and the path to did:web

The node's DID is now `did:plc:ggztd5hjk3cnkhgzdk4rmqan`, minted by its own
self-hosted PDS (`napa-node-01.watershed-agent.dev`) rather than borrowed from a
Bluesky account signup — a real step forward, since the identity no longer depends
on Bluesky-the-company's infrastructure. Synthesis's DID
(`did:plc:clcw2dxrd6qma45gy3oozjwa` / `napasynth01.bsky.social`) is still
Bluesky-issued, appropriately — it's the one identity in this system with an
actual human-facing purpose.

**The onboarding problem itself is unchanged, though.** Both DIDs were still
bootstrapped by a person running an account-creation command by hand (`curl` to
`com.atproto.server.createAccount`, or Bluesky's signup flow) — an agent's
birthright identity shouldn't require a human to click through or type a command
at all. `did:plc` is also still a step short of `did:web`: it depends on a
third-party PLC directory (`plc.directory`, itself Bluesky-run) for resolution,
even though it no longer depends on Bluesky for hosting or the account layer.

**These DIDs are still effectively placeholders** for the eventual Agent Identity
Registry — the ATProto publishing pipeline stays intact regardless of which identity
system sits underneath. When the registry is ready, the reconciliation path is:

1. Registry mints new `did:web` DIDs for `napa-node-01` and `napasynth01`
2. Agents register their public keys and charters with the registry
3. `publishers.json` updated to the new DIDs
4. ATProto records going forward are signed by the registry-provisioned keys
5. The self-hosted PDS / Bluesky handle can remain the publishing transport —
   decoupled from the identity layer

The publishing target doesn't change. The identity primitive underneath does.
The Agent Identity Registry is being built in a separate repo — see that project for
the registry design and implementation.

### DID resolution — the plc.directory dependency we didn't remove

Self-hosting the PDS (above) removed the Bluesky *hosting* dependency: the node's
identity and data no longer live on infrastructure Bluesky operates. It did **not**
remove the Bluesky *resolution* dependency, and it's worth being precise about the
difference, because "distributed" in this project's framing has always meant
"mostly" — the honest state, not the aspirational one.

**How `did:plc` resolution actually works.** A `did:plc:...` string is an opaque
hash — unlike `did:web`, it does not encode where to resolve it. Every resolver
has to already know which directory to ask. In this codebase that's a hardcoded
constant, duplicated in two places since the Viewer (JS) and `subscriber.py`
(Python) share no runtime config:

```js
const PLC_DIRECTORY = "https://plc.directory";   // Viewer/index.html
```
```python
PLC_DIRECTORY = "https://plc.directory"          # Synthesis/subscriber.py
```

Querying `https://plc.directory/{did}` returns a DID document — a `service` array
with a `serviceEndpoint` telling you the DID's current PDS. That's the entire
mechanism: one public GET, one JSON field. It's what makes the "identity outlives
its hosting" property real — the DID is permanent, the PDS location is a mutable
field in a document the DID owner controls, and moving hosts requires zero
changes to anyone else's code, only an update to that document. `subscriber.py`
resolves each trusted publisher's DID independently rather than assuming they
share a PDS — the fix that actually makes multi-node fetch correct (see
"Synthesis agent" above); before that, a single global `PDS_HOST`/`ATPROTO_PDS_URL`
would have silently missed a second node's records if it ran its own PDS.

**What that document's integrity rests on.** Every operation in the PLC log —
create, rotate keys, move PDS, tombstone — is signed by the DID's own rotation
key, not merely attested to by whoever runs the directory. The server
(`did-method-plc`) is open source and the operation log is exportable, which is
a deliberate mitigation: the *data* isn't proprietary, so anyone holding a synced
copy could stand up a faithful replacement and serve the exact same mappings,
verifiable independent of who's hosting them.

**What isn't mitigated: resolution continuity.** There is no discovery mechanism
between directory instances — no DNS-style delegation, no fallback list, nothing
in the protocol that lets a resolver try a second directory if the first is
unreachable. In practice there is exactly one directory basically everyone in the
ATProto ecosystem resolves against, run by Bluesky PBC. If it went dark with no
warning:

- Every `did:plc` DID in the *entire ecosystem* — not just this project — becomes
  unresolvable for anyone without a cached copy, all at once. This is a systemic
  ATProto risk, not something specific to Agentic Watershed.
- Recovery depends on someone already running a synced mirror (or having exported
  the log pre-shutdown) standing up a replacement, and then every piece of client
  code in existence — including our one hardcoded constant — being manually
  repointed at it. Slow, uncoordinated, ecosystem-wide, not automatic failover.
- **New operations go dark before the mirror problem even matters.** Existing DID
  documents stay resolvable (read-only) as long as *some* copy of the log is
  served somewhere, but rotating keys or moving a PDS requires submitting a fresh
  signed operation to a directory actively accepting writes. With none, every
  `did:plc` identity is frozen at its last known state.

**Where this project stands on it, deliberately.** Not treating this as urgent —
the ATProto team is aware of this class of problem and it's a reasonable bet that
resolution federation gets addressed at the protocol level before it's forced by
an outage. But it's real, it's documented here rather than glossed over, and it
sharpens something already noted above: `did:web` resolves the discovery problem
*by construction* (the DID literally is the address — no shared directory at
all), which is the strongest concrete argument yet for eventually moving the
node's identity there. Two lower-effort mitigations worth doing before that, if
this becomes a live concern:

1. **Cache the last-resolved PDS URL** (Viewer, subscriber) instead of always
   live-resolving via `plc.directory` on every call — degrades a directory outage
   to "stale but working" instead of "broken."
2. **Consider self-hosting a PLC directory mirror** — `pds.env.example` already
   flags this as "possible but out of scope," which is still the right call for
   now, but it's the concrete next step if the shared dependency ever needs
   removing rather than just documenting.

### Watershed agent changes when the registry is ready

The agents need a small Python client module for the registry — three operations:

**`provision(charter) → DID`**
Generates a local keypair, registers the public key + charter with the registry,
returns the DID. Private key stored locally (`~/.agent/keys/{did}.pem`).
Called once at agent setup, not on every run.

**`sign(record) → signed_record`**
Signs an ATProto record with the agent's local private key before publishing.
Replaces the implicit signing via Bluesky App Password.

**`verify(did) → charter`**
Resolves a DID against the registry, returns its charter. Cached with TTL.
Used by `subscriber.py` to replace the static `TRUSTED_PUBLISHERS` dict.

The transition in `subscriber.py`:

```python
# Today — identity check only, static list
if publisher_did not in TRUSTED_PUBLISHERS:
    return

# After registry — identity + capability check, live lookup
charter = registry.verify(publisher_did)
if not charter or "observe" not in charter.capabilities:
    return
```

Trust becomes **capability-aware**, not just identity-aware. A DID that's known
to the registry but whose charter doesn't declare the `observe` capability is
rejected even if its identity is valid. This is the charter model in practice —
the registry doesn't just answer "who is this?" but "is this agent authorised
to do what it's claiming to do?"

**`did:web` is the near-term clean answer.** A DID document is just a JSON file served
at a well-known URL:

```
did:web:cpricedomain.net:agents:napanode01
  → https://cpricedomain.net/agents/napanode01/did.json
```

No signup flow. No human identity in the loop. Agent provisioning generates a keypair,
writes the DID document to the domain, done. Three lines of Python.

**The scaling problem:** `did:web` ties each DID to a URL path — one file (or route)
per agent. Fine at tens, unmanageable at thousands.

**What this wants to be: an Agent Identity Registry.**

A lightweight API at your domain that mints and manages DIDs for agents:

```
POST /agents              → generate keypair, mint DID, record charter → returns DID
GET  /agents/{id}/did.json → serve DID document (did:web resolution endpoint)
GET  /agents/{id}/charter  → serve the agent's charter (capabilities, scope, intent)
POST /agents/{id}/rotate   → rotate keys, update DID document
DELETE /agents/{id}        → revoke — DID document returns tombstone
```

**Implementation complexity: low.** Standard JWK keypairs, SQLite, a tiny FastAPI
app. A weekend project for the core. The hard questions are design:

- **Charter schema** — what capability claims, scope, intent, operator identity fields
  does a charter carry? Probably JSON-LD or a custom Lexicon.
- **Key custody** — the registry should never hold private keys. The agent generates
  its own keypair and registers only the public key. The registry issues the DID and
  records the charter. Closer to a CA than an IDP.
- **Provisioning policy** — who can mint a DID? Open self-service, or does the
  registry gatekeep? If the registry is a trust anchor, this matters.
- **Registry's own DID** — the registry itself should have a `did:web` at the domain
  root. Agent DID documents reference it as their controller/issuer. Consumers can
  verify: "this agent was provisioned by this trusted registry" — chain of trust
  without a central CA.

**This registry is the thing Ping should probably build.** It's an identity provider
for agents — but one that issues birthright DIDs and stores charters rather than
managing human sessions and issuing tokens. The registry's DID is the trust anchor;
the agent's DID document is the verifiable claim that it was provisioned by that anchor.

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

### Where the IDP re-enters — delegated agent authorisation

The charter model handles agent-to-agent trust. But agents also need to act
on behalf of people — and that's where the IDP has a legitimate role.

The flow:

1. **Person authenticates** to an IDP (Ping, in this case) in the normal way
2. **Person delegates** to an agent DID for a specific scope:
   "I authorise `napasynth01` (DID: `did:plc:...`) to access my environmental
   data on MCP server X, for this purpose, for this duration"
   — that delegation is a verifiable credential, signed by the person's identity,
   referencing the agent's DID
3. **Agent presents** its DID + the delegation credential to the MCP AuthZ server
4. **AuthZ server issues a token** where:
   - `sub` = the person (the principal being acted on behalf of)
   - `act` = the agent DID (RFC 8693 Token Exchange — the actor)
   - scopes = what the agent is authorised to do on their behalf
5. **MCP server** validates the token, sees both the agent identity and the human
   principal, enforces policy against both

Revocation is clean: revoke the delegation credential. The agent's DID and charter
persist — it's just no longer authorised to act for that person. No token hunting,
no session invalidation. The credential chain is the audit trail.

This is OAuth 2.0 Token Exchange (RFC 8693) and Rich Authorization Requests
(RFC 9396) done with agent-native primitives — where the subject is a DID with
a charter and an observable history, not just an opaque client_id.

**The demo this points toward:** this watershed synthesis agent, with its DID,
its charter (declared in the DID document), and its public record of observations,
asks a Ping-protected MCP server for a token to act on behalf of a user. The
MCP AuthZ server (the one wired into this session) is exactly the thing that needs
to understand that exchange. The IDP handles the human side. The DID chain handles
the agent side. The AuthZ server holds them together.

This is the bridge between the agentic watershed work and Ping's core product.

### Possible additions
- Additional Pi nodes upvalley with their own domain agents and DIDs
- Physical sensors via Pi GPIO → same collector interface, no agent changes needed
- Additional USGS stations (Conn Creek, Milliken Creek tributaries)

### Next: move Synthesis agent from reactive to predictive

**The goal:** the agent should notice trends, anticipate risk windows, and eventually
compare its predictions against outcomes — building a track record over time.

Four phases, each buildable independently:

---

**Phase 1 — Better context ✅ DONE (2026-06-23)**

Shipped in `agent_atproto.py`:

- **Deeper memory**: `read_recent_synthesis()` now returns 14 observations (7 days of
  twice-daily runs) presented oldest-first as a timeline. The agent can now see weekly
  drift, not just recent state.

- **Seasonal calendar**: `seasonal_context()` function computes and injects the current
  date, fire season status (day N of 183), days until Diablo wind season onset, and
  flood season status into every prompt. Anchors the model in the actual calendar year.

- **Domain trajectories**: per-type observation cap increased from 5 → 6. Domain
  observations are trimmed of noise fields (publisher DID, agent model) so the token
  budget goes to signal.

- **Updated system prompt**: explicitly instructs the agent to reason about trajectory
  and flag developing trends even when current conditions are still benign.

---

**Phase 2 — Explicit trend calculation ✅ DONE (2026-06-23)**

Shipped in `agent_atproto.py`:

- `compute_trends()` function extracts numeric fields from `raw_record` JSON in
  `subscriber.db` for each domain and computes deltas across the observation window.

- Metrics tracked (field names match `ATProto/publisher.py`'s actual output as of
  2026-07-02 — the original field names here were speculative and never matched
  what got published; see "Known issues"):
  - Watershed: `dischargeCfs`, `gageHeightFt` (superseded 2026-09-07 by
    `dischargeMeanCfs` and `gageHeightMaxFt`; the old names are still read as
    fallbacks so a trend window can span the change)
  - Weather: `temperatureF`, `humidityPct`, `windSpeedMph`, `precipMm24h`,
    `windDirectionDeg` (with Diablo quadrant detection), `windPattern`
  - AQI: `pm25Aqi`, `ozoneAqi`

- Each metric shows: old → new value, delta, hours elapsed, direction, and a plain-language
  risk note (e.g. *"humidity: -18% over 12h, falling — ⚠ fire risk building"*).

- Diablo wind detector: flags automatically when `windDirectionDeg` enters the 22°–112°
  NE/E quadrant with `⚠ DIABLO QUADRANT (NE/E offshore flow)`.

- Trends section is inserted after the domain observations in every prompt. If fewer than
  2 data points exist per domain, the section is omitted gracefully.

---

**Phase 3 — Prediction and outcome tracking ✅ DONE (2026-06-23)**

Prediction ledger is live. Key design decisions encoded as constants (not prompt-implied):

| Constant | Value | Meaning |
|---|---|---|
| `FLOOD_ACTION_STAGE_FT` | 12.0 ft | Gage height that confirms a flood prediction |
| `AQI_USG_THRESHOLD` | 100 | PM2.5 AQI that confirms an air quality prediction |
| `FIRE_CONFIRM_LEVELS` | `{high, extreme}` | `weather.fireRisk` values that confirm fire |
| `FIRE_CONFIRM_ALERTS` | Red Flag Warning, Fire Weather Watch | NWS alert names |
| Horizons | fire=48h, flood=72h, air_quality=24h | Auto-expiry windows |

`predictions` table added to `synthesis.db`. On each run:
1. `check_predictions()` runs at the **top** of `gather_context()` — resolves open
   predictions against current observations, marks expired ones `expired` (not left
   as `pending` indefinitely).
2. Compact ledger summary injected into every prompt (counts + 5 most recent).
3. `write_predictions()` called **before** `write_observation()` — ledger survives
   a crash between agent and publisher steps.

Also fixed in this session:
- `--lookback 13 → 15` in `entrypoint.sh` (safer overlap for clock drift/startup delay)
- `_is_diablo()`: full meteorological rationale in docstring (22°–112° not a magic number)
- System prompt: handles absent trends section explicitly; explains prediction ledger to agent

First real calibration test: Diablo wind season (September–November 2026).

**Known limitation — confirmation signals still use `flagged`, not numeric fields:**
Prediction resolution currently confirms via the domain agent's `flagged=True` field
rather than specific numeric thresholds (`FIRE_CONFIRM_LEVELS`, `FLOOD_ACTION_STAGE_FT`,
`AQI_USG_THRESHOLD`). This was originally deferred because the publisher only wrote
`summary`/`flagged` to ATProto records — **that's no longer true as of 2026-07-02**;
numeric fields (`temperature_f`, `dischargeMeanCfs`, `gageHeightMaxFt`, `pm25Aqi`, `ozoneAqi`,
etc.) are now populated on every record. Threshold-based confirmation in
`_resolve_prediction()` is a live option now, just not wired in yet — the constants
are still sitting unused in code, waiting for that change.

**What this project actually is:**
Agentic-Watershed is not primarily a fire/flood prediction system — prediction
accuracy is a secondary concern. The project is an exploration of distributed agent
architecture: how agents at the edge (Pi nodes) publish structured, DID-signed
observations; how a separate agent in the cloud (Azure) subscribes, reasons across
domains, and publishes advisories; and how identity, trust, and data flow across
that boundary without central coordination. The environmental domain is the vehicle,
not the destination.

---

**Phase 4 — Calibration (post-autumn)**

Once Phase 3 has accumulated a season of data:

- Track precision/recall by risk type and season.
- Feed a calibration summary into the system prompt.
- Seasonal recalibration: a separate monthly job updates the calibration summary
  on the Azure File Share, read by the synthesis agent on each run.

*Changes needed:* calibration job (new cron), calibration summary file on Azure
File Share, system prompt update to include it.

---

## The prediction ledger was a mirror (2026-09)

The synthesis agent had been reporting extreme fire risk on every run for over
two weeks, including on mornings of 93.7% humidity and no wind. Its own prompt
tells it to calibrate against the prediction ledger — "many false positives
means you are being too aggressive; raise your threshold" — so the obvious
question was why the ledger wasn't correcting it.

It was, in a sense, doing the opposite. `_resolve_prediction()` confirmed a
prediction whenever the corresponding domain agent's top-level `flagged` was
true, on the reasoning that the agent's own assessment is authoritative. Fire
maps to the Weather agent, and the Weather agent flags on nearly every run. So
synthesis predicted extreme fire risk, Weather flagged for its own reasons, the
prediction was marked confirmed, and the ledger reported a perfect record: 128
confirmed fire predictions, one pending, zero expired, zero false positives.
One of the confirmations carries the resolution note "a marked calm".

The agent then cited that record back as justification. From the 2026-09-06
18:00 run: "Prediction ledger shows high confirmation rate (59/65 confirmed,
only 4 expired) validating continued extreme fire risk assessment — no reason
to lower threshold at this time."

Air quality looked healthier — 27 confirmed against 11 expired — but not
because its resolution differed. It used the same `flagged` check. The AQI
agent simply flags honestly, because PM2.5 is usually Good, so its predictions
were free to expire. Weather never stops flagging, so fire's never could.

The thresholds needed to fix this already existed as module constants, with a
comment saying "encoding thresholds as constants prevents drift — the agent
can't argue itself into a looser definition of confirmed", and the function's
own docstring noted they were "not yet wired in". Resolution now reads the
numbers in the record. Two other things came out of wiring it up: only
observations that postdate a prediction can confirm it (any record in the
lookback window used to count, so a prediction could be confirmed by data older
than itself), and neither fire constant could be evaluated at all, because
`weather.fireRisk` was never emitted by the publisher and `activeAlerts` was
hardcoded to `[]`. The alerts half of that is fixed as of 2026-09-07 —
`FIRE_CONFIRM_ALERTS` now has something to match. `fireRisk` stays absent, and
should: the weather agent returns only summary, flagged and reasoning, so
publishing a risk level would mean inventing an assessment and attributing it
to the agent.

Purged a second time on 2026-09-08, for an unrelated cause. The wind unit bug
meant `_confirms` had been resolving fire predictions on gust figures 3.6x too
high, so the ledger began refilling with false confirmations immediately after
the first purge. `LEDGER_VALID_FROM` moves to `2026-09-08T12:00Z`.

The cutoff alone would not have held. Published ATProto records are immutable,
so the inflated wind is in them permanently, and the subscriber's 15-hour
lookback keeps them in view long after the collector was fixed — a purge would
have cleared the past and let the same contamination straight back in.
`WIND_DATA_VALID_FROM` therefore makes wind from observations predating the
fix confirm nothing, keyed on the observation's own timestamp rather than on
when it was resolved, which is what makes it robust to publish lag, backlog
republishes and the lookback window.

Not a plausibility bound, deliberately: a bound low enough to reject a 70.5 mph
inflated reading would also reject the genuine extreme gusts the rule exists to
detect. The value ranges overlap and cannot be separated by magnitude. Only
provenance separates them, so provenance is what is checked. Humidity and
temperature were never affected, so the humidity-alone rule still resolves
against the whole history and the ledger is not left completely empty.

The 128 legacy confirmations are marked `invalidated` rather than deleted, and
excluded from the ledger the agent reads. Leaving them to age out over 30 days
meant a month of the agent quoting a record the bug had manufactured. The
ledger summary now also tells the agent the reset happened and that a short
history is not itself evidence of accuracy — without that it would see a
suddenly thin ledger after days of reasoning from an unbroken record and reach
for an explanation, which is the failure this whole thing is about.

### The wind correction read as weather (2026-09-08)

The first synthesis run after the unit fix reported "the 48h max wind has
dropped from 66.3 mph to 18.4 mph" and built a story on it: "genuine
short-term moderation", "second consecutive run showing conditions actually
improving", "a temporary lull, not a trend reversal". 66.3 / 3.6 = 18.4
exactly. It was narrating the unit correction as meteorology, and waiting for
wind that never existed to return.

Two sources fed it. `compute_trends` compares the oldest and newest record in
the window, and with a pre-fix record at one end it computed the correction as
a 72% collapse in wind speed and handed it to the agent as fact — the ledger
path had been guarded by `WIND_DATA_VALID_FROM` and the trend path had not.
The other source is memory: the agent's own prior write-ups quote inflated
figures, and prose carries numbers that nothing re-derives.

Both are now handled, and the shape of the fix is the same in each: state the
provenance rather than hide the gap. The trend section says wind is not
compared and why; the memory section says how many of the runs shown predate
the fix. An absent number with a reason is worth more than a number the reader
cannot date.

### Numbers the harness could not substantiate (2026-09-08)

The same record claimed "day 147+ of a historic drought" and "147+ consecutive
precipitation-free days, unprecedented per history". Nothing could have
produced that figure: `DRY_SPELL_LOOKBACK_DAYS` was 7, so the deepest true
statement available was "none in the last 7 days". It was a prose counter
incrementing itself across runs, the same pattern already recorded here as
143+ → 143+ → 145+.

Two things were wrong, not one. The counter was invented, and the framing was
also wrong — a rainless Napa summer is the seasonal norm, and the agent's own
seasonal calendar says "Flood season: not active (dry season)". It overrode
computed seasonal context with a number it had made up, and that number was
the primary justification for holding fire risk at extreme.

Fixed by computing it instead: the dry-spell search now runs over the whole
observation record, and publishes `daysSinceMeasurableRain` when rain is found
or `dryRecordDays` when none is, never both. The distinction is the point —
`dryRecordDays` means "at least this long, and the collector cannot see
further back", which is a bounded claim rather than a drought length. The
system prompt now requires the "at least N days" form and states that
consecutive rainless days between May and October are expected.

Worth noting what the same record got right: "Diablo wind season begins in 7
days" was exact, because `seasonal_context()` computes it. Every reliable
number in that record came from code and every unreliable one came from prose.

### Discharge is a rate, not a volume (2026-09-08)

The same record turned "St. Helena 0.0 cfs" into "zero watershed water
availability for firefighting" and "zero watershed reserves". Neither follows.
Zero discharge means no measurable flow, not an empty channel — the same
record carried 0.47 ft of gage height, which is standing water — and Napa
firefighting draws on reservoirs and municipal supply, which this system does
not observe at all. Gage height was available and went unread; the reasoning
ran entirely on cfs.

`compute_trends` now emits an explicit channel-state line when discharge is
near zero and stage is above it, and the system prompt states that discharge
is a rate and that water supply is outside what this system measures. The
computed line is doing the real work: telling the agent not to infer something
is weaker than showing it the number that contradicts the inference.

### A controlled burn on an island justified extreme risk (2026-09-09)

The clearest single demonstration of what this pipeline cannot do. FIRMS
detected roughly fourteen VIIRS pixels on Angel Island in San Francisco Bay,
peaking at 66 MW, one NOAA-21 overpass on 2026-09-08. Every part of the
detection was correct: real fire, real thermal output, distance and confidence
computed properly, inside the currency window. Synthesis escalated to extreme
fire risk for Napa Valley on it.

It was a controlled burn, on an island, with no fuel path to the valley at
all. The nearest actual wildfire that day was a small one at Willits — 96
miles north-northwest and outside the monitored bounding box entirely, so the
system was structurally blind to it. The one real fire was invisible and the
one non-threat drove the verdict.

Two separate gaps, worth keeping distinct.

The first is that a thermal anomaly has no type. A wildfire, a prescribed
burn, an agricultural burn and a refinery flare are identical in this data.
The fix is a second source — a named-incident feed — which was deliberately
deferred when Fire was built, on the reasoning that it was worth doing "once
FIRMS itself is proven in production". It is now proven, including proven to
need corroboration.

The second is that distance is not a threat model. The rules use 20 miles
unconditional and 50 miles for high confidence, and those are reasonable
*detection* bounds. Whether fire can reach the valley depends on terrain, fuel
continuity and water in between, none of which is observed. An island thirty
miles across open water and a ridge thirty miles upwind on continuous chaparral
produce the same record. Rather than model propagation, both prompts now say
plainly that a detection is not a threat and that the agents must not assert a
danger this data cannot establish.

A third, smaller finding came out of the same record: the agent called 66 MW
"well beyond anything previously reported in FRP magnitude" when its own table
held 64.0 MW two weeks earlier and 53.2 MW nine miles out. It was the highest
of seven comparable readings in two months, about 3% above the prior peak. The
tools returned a raw MW figure and no distribution, so the superlative was a
guess — the same species as the drought counter. `get_nearest_hotspots` now
returns an `frp_context` block and per-hotspot percentiles, and the publisher
emits `nearestHotspotFrpPercentile` so Synthesis inherits the baseline instead
of inventing one.

The persistent fixed source that does exist in the data, and that nobody had
noticed, is at 38.002/-121.934 — eight detections from 2026-07-18 to
2026-09-08 averaging 0.8 MW at 28 miles, in the Pittsburg industrial corridor.
It has never mattered because 0.8 MW triggers nothing.

### CAL FIRE incidents as a second source (2026-09-09)

Added after the Angel Island escalation, and the deferred fast-follow finally
taken. `Fire/incidents_collector.py` polls
`incidents.fire.ca.gov/umbraco/api/IncidentApi/List` hourly into an
`incidents` table in `fire.db`, and the MCP server matches hotspots to named
incidents.

Matching is a POSITIVE identifier only. A match names a detection and gives
its acreage and containment. An absence establishes nothing: not that the
detection is harmless, not that it isn't a fire, only that no published
incident corresponds. An incident must be reported, confirmed and published
before it can appear, while a satellite sees the heat immediately, so there is
always a window in which a real fire is detected and unlisted. Prescribed
burns may never be listed at all. Every layer says this in those terms — the
tool's `incident_note`, the lexicon description, and both prompts — because
the tempting misreading is "unmatched, therefore fine", which would be a worse
failure than the one being fixed.

A correction worth recording in full, because the reasoning error is the
recurring one here and the resolution is instructive.

The first version said CAL FIRE "publishes notable incidents, not a census of
fires", inferred from the Willits fire being absent from the feed. That
inference was wrong: `?inactive=false` returns only currently-active
incidents, so absence there was expected and proved nothing — the same shape
as reading a Watch Duty check on the 9th as evidence about a detection from
the 8th. The claim was rewritten to rest on publication lag alone, which holds
regardless of coverage.

Then the full feed settled it, and the original claim turned out to be true
for a reason the original evidence never supported. `?inactive=true` returns
484 incidents for all of 2026, 4 of them active, in a state with thousands of
fires a year. The Willits fire is not in it at all — the nearest Willits-area
record is the Ponderosa Fire, 5 acres, from nearly a month earlier. Types are
Wildfire (472), Fire (11) and Hazmat (1); there is no prescribed-burn
category, so a controlled burn can never match.

So the caveat now names both gaps, because they are independent and either
alone would be enough: publication lags ignition, and the feed is curated
rather than complete. Being right for the wrong reason is still being wrong,
and the fix was to go and get the data rather than to argue the inference.

That correction changed the polling design too, and the two are the same
decision. The collector polls `?inactive=true` rather than `?inactive=false`,
because incidents drop out of the active feed the moment they close: polling
active-only would have to run often enough to catch a short-lived fire inside
its own lifetime and would still miss anything closed before the first run.
Fetching everything makes each poll a complete picture rather than a sample,
which is what lets the cadence match consumption — fire records are built
twice a day, so the collector runs twice a day at 2:50 and 14:50 rather than
hourly. The full feed is 321KB, so twice-daily costs nothing. Every record in
it carried a 2026 start date, so the feed appears to be current-year only,
which makes the local table the only place last year's incidents will survive
a year rollover.

Two details worth keeping. The match radius scales with the burn rather than
being flat: an incident is a published point and a fire is an area, so the
tolerance is the equivalent circular radius of the acreage plus fixed slop for
the point being a label. Plaskett at 29,884 acres gets 8.9 miles; a one-acre
incident gets 5.0. And rows are kept forever and upserted on CAL FIRE's
`UniqueId`, because incidents drop out of the active feed once they close and
a three-day-old hotspot must still be matchable against an incident that has
since gone inactive. `first_seen_at` survives updates; acreage and containment
take the latest value.

`nearestIncidentName` and `nearestIncidentDistanceMi` are published
independent of any hotspot, deliberately not bounded by the FIRMS box. That is
the fix for the second half of the Angel Island day: the real fire at Willits
was 96 miles out and outside the box, so the satellite feed could not see it
at all.

Each row also stores its raw JSON. This is a CMS endpoint, not a versioned
API — the field names are whatever Umbraco serialises, `Name` arrives padded
with a trailing space, and `PercentContained` is null on new incidents. If the
mapping turns out wrong, the raw column means it can be corrected without
re-polling history the active feed will no longer serve.

### Distance alone was not a match (2026-09-09)

The first real incident poll on node-01 exposed a defect the design review had
missed. The five incidents nearest Napa in 2026 are all inside the 20-mile
unconditional radius — Mason at 7.5 miles, then Lyon, Ruth, Pablo and Petersen
between 16 and 17.6 — and every one is 100% contained.

The matcher compared distance only. A new detection near any of those
locations would have been published as "Mason Fire, 19.5 acres, 100%
contained": a fresh fire wearing the identity of a closed one. That is the
same failure as the five-day-old hotspot published as current, except pointed
the other way — instead of manufacturing alarm it would have manufactured
reassurance, which is worse.

A hotspot must now fall inside the incident's burning period as well as near
its location. The window is padded at both ends for opposite reasons: a
satellite sees heat before an incident is reported and published, so a
detection can legitimately precede the recorded start; and ground stays hot
after containment, so one can legitimately follow the end. An incident with no
usable start date is allowed through on distance alone rather than dropped,
since it is still a named incident and the dates travel with the match.

It also corrected an estimate. Before the data arrived, matching was expected
to fire "rarely". Five incidents inside the unconditional radius in one year
means local matching is common, which makes the temporal test load-bearing
rather than defensive.

### A prompt cannot ask for a tool call these agents can't make (2026-09-09)

The incident cross-reference shipped with `get_active_incidents` never being
invoked. The Fire prompt said "Also call get_active_incidents", which reads
fine and does nothing: these agents assemble their own context in
`gather_context()` and hand the model a single forced `submit_assessment`
tool. The model has no free tool choice, so an instruction to call something
is inert. The first dry run after deploy showed five tools in the log and no
incident section anywhere in the output.

Fixed by calling it in `gather_context()` and rewriting the prompt to
reference the section header the model actually receives rather than a tool
name it cannot act on.

Now a checked invariant: every `get_*` tool named in an agent prompt must
appear in that agent's `call_mcp_tool` list. River, Weather and AQI name none
and were never affected; Fire names two and calls seven.

The general shape is worth remembering when adding a tool to any of these
agents: writing the tool, wiring it into the MCP server, and describing it in
the prompt are three steps, and none of them makes it run.

### A real fire, and the rule that didn't cover it (2026-09-09)

The Steele Fire started at 16:59 UTC, 14.1 miles from the node in Napa County,
and appeared in the CAL FIRE feed 50 minutes later — an unusually short
publication lag. It is the first live test of the incident source, and the
exact inverse of Angel Island: there, a detection with no incident; here, an
incident that may never produce a detection.

It exposed a gap the incident work had left. Every flag rule was
hotspot-based, so a confirmed, named, actively-burning fire inside the
unconditional 20-mile radius produced no flag at all unless FIRMS happened to
see it — and a ten-acre fire may not, since VIIRS pixels are 375m across with
two overpasses a day. The rules had been written when FIRMS was the only
source and never revisited when a second one arrived.

Adding a tool, wiring it into the server, and describing it in the prompt does
not make it a criterion either. That is the same shape as the tool that was
never called, one level up: the incident data was reaching the agent and still
could not change the verdict.

An active named incident within 20 miles now flags, in both the prompt
criteria and `flag_rules.py`. It earns the same unconditional radius as a
hotspot rather than a stricter one, because a confirmed fire is stronger
evidence than an unattributed thermal anomaly, not weaker. The rule is guarded
separately so a missing `incidents` table cannot take the other five down.

The agent's charter was widened to match. It had said its "only job" was
satellite-detected heat, which was true when written and wrong once a second
source existed. It now states the question — is there a fire near Napa Valley
right now — and names both sources along with how each fails: FIRMS is
immediate but cannot identify what it sees and misses small fires; CAL FIRE
names and confirms but lags publication and covers only a subset. A fire
visible to one may be invisible to the other.

### A public advisory published from no data (2026-09-09)

The 18:00Z synthesis run received zero observations. Its reasoning was the
best this system has produced — it identified five of its own prior errors by
name, refused to carry the invented drought counter forward, correctly
reattributed the Angel Island detection, and cited the ledger expiring a
prediction rather than auto-confirming it, which was the first honest
resolution. All of that was reasoning about its own history, because it had
no present to reason about.

Three defects turned a transient fetch failure into a published risk
assessment, and none of them was the fetch failure itself.

The subscriber exited 0 having fetched nothing. `entrypoint.sh` runs under
`set -e`, so a non-zero exit would have halted the pipeline before the agent
ran; instead the agent synthesised from an empty database and the publisher
posted an advisory to Bluesky. A network fault and a quiet afternoon were
indistinguishable to every layer above. `run_fetch` now returns an exit code
and distinguishes the two deliberately: an unreachable publisher means we do
not know and must not guess, so the pipeline stops; an empty window from
publishers we did reach is a fact about the world and passes through, loudly
logged, so the record can state it.

`domainsObserved` was hardcoded to all four domains. The record therefore
asserted watershed, weather, aqi and fire had all contributed to a run whose
own prose opened "received no node observations at all". The prose was right
and the structured field was not, which is the worse way round: a consumer
parsing the record never reads the prose. The agent now records which domains
actually arrived and how many nodes they came from, and the publisher reports
those. An empty list is a real assertion and is published as one. Rows from
before the columns existed also report empty, because the honest answer to
"which domains contributed" is never "all of them".

`publishers.json` was never copied into the Synthesis image. It worked only
because `subscriber.py` falls back to a single hardcoded DID that happens to
be correct. Adding a second node to that file would have changed nothing in
the deployed job and nothing would have said so. Now copied, and the
subscriber logs which registry it loaded — or that it found none and is using
the built-in default.

The common thread with the rest of this week: the failure was not that
something broke, it was that nothing downstream could tell it had.

### A one-second 502 cost a synthesis run (2026-09-09)

The 18:00Z run's execution log settled what four other checks could not:

```
GET https://plc.directory/did:plc:ggztd5... "HTTP/1.1 200 OK"
Fetching records from napa-node-01 via https://napa-node-01.watershed-agent.dev...
GET .../listRecords?... "HTTP/1.1 502 Bad Gateway"
Failed to fetch from napa-node-01: Server error '502 Bad Gateway'
=== Fetch complete — 0 fetched, 0 new ===
```

Azure's egress was fine, DID resolution succeeded, and Cloudflare answered. A
502 from a tunnel means the edge was reachable and the origin was not — the
PDS on the Pi, or cloudflared's link to it, failed for one second at
18:00:22Z. It was serving normally before and after.

Worth recording that the diagnosis before the log was wrong. Having ruled out
the PDS, the publisher, the record window and the DID, the conclusion drawn
was "the fault is on Azure's side of the connection". It was the opposite end.
Everything that had been eliminated was eliminated correctly; the remaining
inference was still backwards, because "not any of the things I checked" is
not the same as "the thing I did not check". The log took one query and
settled it.

The subscriber had no retry at all. A 502 is the canonical retryable case —
the request was valid and the server could not answer it right then — and
losing a twice-daily run to a one-second blip is the wrong trade. It now
retries 5xx, 429 and transport errors up to four attempts with 2/4/8s backoff,
and deliberately does not retry 4xx: a 400 or 404 is a statement about the
request, and repeating it changes nothing.

This pairs with the exit-code change. Retry handles the blip; the non-zero
exit handles a real outage. Before, both produced the same silent zero.
### The numeric block described a later moment than the record (2026-09-09)

An AQI record carried `pm25Aqi: 52` while its own summary said "PM2.5 AQI at
43 ... as of 2026-09-09 06:00 UTC". Two separate causes, and the record only
looked wrong because both landed at once.

The numerics fetchers had a lower bound and no upper one: `collected_at >=
cutoff`, then `ORDER BY ABS(collected_at - observedAt)`. A reading taken
*after* observedAt could therefore win — from a 15:00:15 observation, a 15:15
reading is fifteen minutes away and beats the 14:45 one the agent actually
saw. The agent wrote 43 from what existed when it ran; the publisher, running
an hour later, attached 52 from a reading that did not exist yet. All three
fetchers are now bounded at observedAt, so a record can only contain data that
existed at the moment it claims to describe.

Worth noting the inconsistency that hid it: `_fetch_dry_spell` and
`_fetch_active_alerts` were both written with an upper bound from the start.
The three older fetchers were not, and nothing compared them.

The second cause was a unit. AirNow's `HourObserved` is an integer in the
reporting area's LOCAL time, exposed to the model as a bare `obs_hour`. The
agent read 6 and wrote "06:00 UTC", moving the observation seven hours. Same
failure as reading NWS `windSpeed` without its `unitCode`: a value handed over
without its unit gets given the wrong one. The field is now
`obs_hour_local`, the tool explains that `collected_at` is UTC and
`obs_hour_local` is not, and the prompt says so too.

The general rule this keeps producing: every timestamp and every measurement
needs its frame attached at the point it leaves the database, not inferred
downstream. Four separate bugs now — wind units, hotspot currency, AQI clocks,
and USGS qualifier codes — have been the same omission.

### The fourth instance: USGS qualifiers and escaped labels (2026-09-09)

Two River defects, both visible in one line of collector log that had been
printing unremarked for months:

```
Napa River near Napa | Streamflow, ft&#179;/s | 0.14 ft3/s ⚠️
```

`ft&#179;/s` is USGS's `variableName` arriving HTML-escaped and stored
verbatim. It was never confined to the log: `mcp_server.py` returns
`parameter_name` from four of its five queries, so `&#179;` was reaching the
agent's context as raw markup and the model had to infer it meant a cubed
superscript. Fixed with `html.unescape` at the parse site plus a
`schema_migrations`-guarded backfill, the same shape as the wind-unit
correction — the decode is exact and reversible, so leaving old rows would
only mean a trend window treating `ft&#179;/s` and `ft³/s` as two units.

The ⚠️ was worse for being subtler. It was keyed off `bool(qualifier)`, and
every real-time USGS value carries `P` for provisional, so the marker fired on
every reading and therefore meant nothing — while `Ice`, `Eqp` and `Bkw`, the
codes that say the measurement itself is compromised, rendered identically to
routine data. The codes also reached the agent as bare letters with no
glossary in any tool docstring, prompt, or output: the model was being asked
to know NWIS conventions from memory. `River/qualifiers.py` now holds the
routine/condition split once, read by the collector's log and by
`_rows_to_dicts`, which annotates every row with `qualifier_meaning`. An
unrecognised code passes through verbatim and counts as notable — the same
"never guess" rule Weather applies to an unfamiliar `unitCode`.

Worth noting how both were found. Neither came from a check; they came from a
reboot. The Pi was rebooted after a sluggish afternoon, the Viewer failed, and
four rounds of diagnosis proved every hop healthy — the tunnel had started on
boot, the PDS was serving, plc.directory and bsky.social both answered, and a
hard refresh fixed it. The defects surfaced only because a collector log was
on screen for an unrelated reason. That is now the pattern for every defect
found here: a human looked at real output.

The same session produced an operational lesson worth keeping. `crontab -e`
hung, the crontab was hand-edited instead, and nine lines lost the leading
`. ` of `. /etc/environment` — leaving `/etc/environment && cd ...`, which
tries to execute a 644 data file, fails, and short-circuits the whole job on
`&&`. Every collector and agent stopped. Nothing reported it; the node simply
produced no records, and the only reason it was caught within the hour is that
someone was already looking. A node that cannot say it has stopped is the
same gap as an agent that cannot say its input was missing.

### A field that never worked, and a word that didn't exist (2026-09-10)

Two defects in the 2026-09-10 06:07 synthesis record, both structural.

`nearestIncidentName` was selected by `rows[0]` over every incident in the
table, ordered by distance, with no state filter. CAL FIRE's feed holds the
whole year — 483 closed incidents against typically zero or one burning — so
it named a dead fire on essentially every run. That run reported the Mason
Fire at 7.45 miles as evidence of "multiple active fire signatures in the
region". The Mason Fire burned for eight hours on 2026-06-18 and had been out
for three months.

The field could never have done its job. It was added for the Willits case:
a real fire 96 miles out, outside the FIRMS box, invisible to the satellite
feed. But nearest-by-distance across all history puts any nearby old fire
ahead of a distant burning one, so Mason at 7.45 always beat Willits at 96.
Filtering to burning incidents is not a restriction on that purpose, it is
what makes it possible — there is now a test asserting exactly that ordering.

Note what the same function already did correctly. `nearestHotspotIncidentName`
filtered through `_was_burning` twelve lines below. The temporal check existed
and was applied to one field and not the other. The new `_is_burning_at` is
deliberately separate rather than a reuse: `_was_burning` allows a tail after
containment because a satellite can see heat in a scar, which is right for
matching a detection and wrong for "is there a fire there now".

The second defect was in the vocabulary. `floodRisk` had `knownValues` of
none/low/moderate/high/extreme and the `submit_assessment` tool carried the
same list as a hard `enum`, so the API itself forbade the honest answer. With
`domainsObserved` reading `["aqi", "fire"]`, the agent still had to emit a
flood level, and the least alarming available was `none` — so the record said
"No flood risk; watershed at normal seasonal late-summer lows" having seen no
watershed data at all. The reasoning shows it half-caught this, declining to
cite a drought day-count "not in the observations" while asserting the river's
state in the same breath. It was not being careless. It had no way to say
nothing.

`unknown` is now in the enum, the lexicon, the prompt, the persistence
defaults, the failed-run fallback, and the Viewer. That last one mattered more
than it looks: `riskValue()` rendered a missing value as `none`, so even a
correct `unknown` would have displayed as the calmest reading on the page.

The prompt gained a DOMAIN COVERAGE section that names every domain and says
whether it arrived. Previously an absent domain simply had no section, and a
missing section is not a fact — asking a model to notice a gap is asking it to
observe nothing. The two fixes are the same fix at different layers: make
absence something the system states rather than something a reader must infer.

Both were found the same way as everything else here — a human read a
published record. The shadow rules could not have caught either, because both
fields were internally consistent with the data they were built from.

### Nearest is not strongest (2026-09-10)

The Steele Fire record at 2026-09-09 22:00 published `nearestHotspotFrpMw`
7.47 at the 79th percentile with confidence `n`, while its own summary
described the same 14.0-14.4 mile cluster as holding four high-confidence
detections at 24.1-33.5 MW and the 96th-98th percentiles. Both were true.
They were different hotspots four tenths of a mile apart.

Every structured fire number was keyed to the single nearest-by-distance
detection, and within a cluster which one is nearest is close to arbitrary.
So the machine-readable half of the record understated an active wildfire by
4.5x in FRP and 19 percentile points relative to the prose half, and a
consumer reading only the fields — the Viewer, a third party, any future code
path — got the calm version. `maxHotspotFrpMw` and its distance, confidence
and percentile are now published from exactly the population `nearest` is
drawn from.

The scope constraint is the point, not a detail: two numbers taken from
different sets cannot be compared, which is the AQI clock bug in another
form. The peak is emitted even when it *is* the nearest hotspot, because
omitting it then would make absence mean "same as nearest" — a fact the
reader would have to infer, which is the failure this whole file is about.

Worth recording the objection that shaped it. The first proposal was larger,
and the push-back was "isn't synthesis's job to take the domain observations
and reason?" That is right, and it draws the line: a record publishes what
was measured and leaves what it means to the consumer. `maxHotspotFrpMw` is a
number already sitting in the collector's table. A field like
`fireIntensity: "high"` would be a conclusion, and would move Synthesis's job
into the publisher. The test for a new field is whether a human with the
database could disagree with it — a measurement, no; a judgement, yes.

There is a second reason to prefer the measured number here, specific to this
system. This file already records the agent choosing, among two available
readings, the one supporting its standing conclusion. Prose saying 33.5 MW
while the numerics say 7.47 is exactly that fork. Publishing the peak removes
the fork rather than adding an opinion.

The same review turned up the architectural reason this matters, which is
worth stating plainly because it is easy to get backwards. Synthesis does not
receive the underlying data — that is tens of thousands of SQLite rows on the
Pi, reachable only through the domain agent's MCP tools. It receives a digest
of a dozen fields. The important property of that digest is authorship:
`build_fire_record` takes `summary` and `flagged` from the model's output,
then calls `_fetch_fire_numerics` and queries the database itself. One record,
two authors — the model wrote the prose, code wrote the numbers, and the model
cannot touch the numbers.

By the mirror test that runs through this file, that makes the numeric block
the only real check Synthesis has. Every other input it reads — domain
summaries, its own memory, its own prior records — was written by a model, so
agreement among them establishes only that models agree.

The Steele record shows both sides. Haiku's fire summary is clean: Steele,
the ENE cluster, the SSW cluster, no mention of Mason. It was closest to the
data and it was right. Sonnet read `nearestIncidentName: Mason Fire` out of
the numeric block and wrote "multiple active fire signatures in the region".
The failure was not that Synthesis had the numbers; it was that it treated a
field as a finding rather than as something to reconcile against the domain
agent's account. The domain agent had the context to ignore a field that made
no sense, and Synthesis could not.

Hence the two-authors rule now in the prompt: when a field and a summary
conflict, state both and say they disagree, rather than picking the one that
supports the standing conclusion. That last habit is documented here already —
34.2% humidity restated as "below fire-weather thresholds" against its own 25%
threshold, and 190-280° winds called "offshore/Diablo" against a prompt
defining Diablo as NE/E. Both errors moved the same direction. The rule makes
the disagreement itself a reportable output, so the model is not forced to
resolve something it cannot resolve.

Still open from the same record: `flagged: true` with `flagReason: ""` on an
observation whose summary opens "ACTIVE WILDFIRE". Anyone filtering the
firehose on flagReason gets an empty string for the most consequential fire
record the system has produced. That remains parked pending shadow-verdict
divergence data, but this is the strongest case yet for unparking it.

### Two windows, one of them imaginary (2026-09-10)

`domainsObserved: ["aqi", "fire"]` on the 06:07 record raised the obvious
question, and the execution log answered it twice over.

The immediate cause was the node, not Synthesis. The subscriber fetched at
06:06 UTC with a 15-hour window opening at 15:06 on the 9th and got exactly
three records — aqi 21:00, fire 22:00, aqi 03:00 — matching "3 fetched, 3
new" and "skipped 763 record(s) outside the 15h window". Watershed runs at
07:00 and 19:00 UTC and weather at 08:00 and 20:00; the 19:00 and 20:00 runs
were *inside* that window, so had they published they would have been
fetched. They didn't exist. Those are 12:00 and 13:00 Pacific — the reboot at
12:32 and the crontab break after it. The outage cost each domain one run,
and their earlier 07:00/08:00 records were already outside the window.

The structural finding was in the same log, two lines apart:

```
Lexicon: ...  |  Lookback: 15h                      ← subscriber, fetching
Model: sonnet  |  Dry run: False  |  Lookback: 24h  ← agent, reading
```

`entrypoint.sh` ran the subscriber with `--lookback 15` and invoked the agent
with no flag at all, so it took its `24.0` argparse default. And subscriber.db
is deliberately ephemeral — the entrypoint restores synthesis.db and
synth_publisher.db from the file share, not that one — so nothing accumulates
between runs. The agent's "last 24 hours" was reading a database that had
never contained more than 15. The 24 was not a window, it was a number in a
log line. A third figure sat in the comment above the call: "since ~13h ago".

Both are now the same value, and it is 27 rather than 15. Watershed, weather
and fire all run on a 12-hour agent cadence, so 15 hours left three hours of
slack and a single missed run erased a domain — which is precisely what
happened. 27 tolerates one missed run per domain.

Making them one variable is not enough on its own, because that is a
convention and conventions drift. The subscriber now writes the window it
actually fetched into a `fetch_meta` row, and the agent reads that and takes
the smaller of it and its own flag, logging when it clamps. So the figure the
agent prints is the figure it can support, whatever anyone passes. Same move
as `domainsObserved` replacing a hardcoded domain list: stop asserting a
number that can be measured.

Worth separating what each half fixed. #77 made a missing domain honest —
`unknown` instead of a fabricated all-clear. This makes it rarer. The system
was already going to say it hadn't seen the river; now it will usually have
seen it.

### Provenance was a reconstruction (2026-09-10)

PRODUCT.md says the Viewer's job is "record-level provenance for every
advisory", for a reader arriving from a Bluesky post asking whether they can
trust it. It was not doing that. It fetched every record from every trusted
publisher and kept whatever had an `observedAt` inside 24 hours of the
synthesis record's own, using a constant in `Viewer/index.html` whose comment
read "matches Synthesis's default fetch lookback window". That comment was
never true: the subscriber was fetching 15 hours, and 27 after the previous
fix. A third copy of the same number, in a third language, asserting an
agreement no code checked.

The consequence was false provenance, in the place designed to establish it.
For the 2026-09-10 record Synthesis read three observations — aqi 21:00, fire
22:00, aqi 03:00 — while the Viewer's 24-hour window would additionally
display watershed 07:00, weather 08:00, aqi 09:00, fire 10:00 and aqi 15:00
under the heading "Underlying Observations". A reader checking whether to
believe "watershed at normal seasonal late-summer lows" would have found a
watershed record apparently supporting it, on a page that renders
`domainsObserved: ["aqi", "fire"]` a few hundred pixels above. The page
contradicted the record it was displaying.

The fix is not to sync the constant. Synthesis records now carry
`sourceRecords`, the `at://` URIs of what the run actually read, and the
Viewer filters the records it already fetched by that set — no extra round
trips, and no window at all. The URIs were available the whole time: `at_uri`
is a column in the subscriber's `observations` table that
`read_recent_observations` simply never SELECTed.

Two distinctions the implementation turns on. Absent and empty are not the
same: `[]` asserts "this run read nothing", which a record written before the
field existed cannot claim, so an empty set is omitted rather than published
as `[]` and the Viewer falls back to the window only when the field is truly
absent. And a cited URI that fails to resolve is now stated — "3 of the 15
records this advisory cited could not be retrieved" — because showing twelve
blocks where fifteen were claimed looks complete while being short.

Third instance today of the same root: `fetch_meta` replaced an assumed
window, `domainsObserved` replaced a hardcoded domain list, and now
`sourceRecords` replaces an inferred evidence set. Each was a number or a set
that could be recorded and was instead recomputed downstream by something
that could not see whether it had got it right.

### Four ways to disappear, none of them audible (2026-09-10)

Tracing why one synthesis record said `domainsObserved: ["aqi", "fire"]`
turned up four distinct causes over two days, all presenting identically as
a domain missing from synthesis:

  1. a hand-edited crontab that lost the leading `. ` on nine lines, so every
     job short-circuited on `&&` and produced nothing
  2. an unclean reboot 75 seconds into a River agent run — cron's own log
     shows the CMD, then `-- Boot --`, and the agent's log line was lost with
     the unflushed write
  3. roughly eleven hours of machine downtime overnight
  4. a Weather MCP subprocess that answered and then failed to exit

Not one announced itself. `proc.communicate(..., timeout=30)` was called bare
in all four agents: a timeout raised straight out of `gather_context()`,
ended the run, wrote nothing, and — since `communicate` does not kill on
timeout — left the hung server behind to be leaked again next run. The
Weather agent had already fetched one tool's output successfully and threw it
away.

The publisher then logged `[weather] Nothing new to publish`, which is the
same line it logs for a healthy domain with nothing new. A crashed agent and
a quiet one were indistinguishable from outside the box, and that is the real
defect — the timeout was only what triggered it. The only reason any of this
surfaced is that a synthesis record said `unknown` and a human asked why,
which is #77 working as the last line of defence with nothing in front of it.

Three changes. `agent_runtime.py` at the repo root now holds the MCP client
once instead of four near-identical copies (the same drift `thresholds.py`
was created to stop — the copies had already diverged cosmetically and all
four carried the same unguarded timeout). It retries once, kills the hung
child, and raises `MCPUnavailable` rather than letting a bare
`TimeoutExpired` escape. A malformed reply is *not* retried and no longer
returns `"[Tool call failed: name]"` — that string reads as data and reached
the model as if it were evidence.

A failed run now writes an `agent_observations` row with `status='failed'`
and the reason, so the gap carries its own cause. It is recorded rather than
merely logged because a log line lives on one machine and is read by a human
who already suspects something, whereas this row is read by the publisher —
the component that noticed nothing for two days. `flagged` stays 0: a run
that computed no assessment must never be able to raise an alert.

And the publisher now distinguishes the two cases in the one place it was
silent, logging the failure and its reason at ERROR, and appending "the agent
has not completed a run since the failure above" to what would otherwise be
the reassuring line. Failure rows are never published — an assessment that
does not exist has no business in the lexicon.

`status` NULL means success throughout, because before this existed a failed
run wrote nothing at all, so every pre-existing row is by definition a
completed one.

### The agent was reading the time of day (2026-09-10)

The River record published on 2026-09-10 said "flow crashed from 0.32 cfs to
0.03 cfs in current reading", "gage height dropped from 2.11 ft to 2.07 ft",
"severe anomalous drying pattern", "severe drought conditions emerging", and
flagged. The readings behind it:

```
20:15  2.04 ft  0.0 cfs        23:00  2.06 ft  0.0
21:30  2.05 ft  0.0            23:15  2.07 ft  0.03
22:15  2.06 ft  0.0
```

Stage was rising, monotonically. Discharge sat at exactly 0.0 for eleven
consecutive readings and then ticked *up* to 0.03. Both headline numbers were
directionally wrong.

Two causes, and the second is the interesting one.

**The rating floor.** Discharge is not measured; it is derived from stage
through a rating curve, and below a certain stage the curve reports exactly
0.00 regardless of the river. Eleven readings of 0.0 spanned a 0.02 ft range
of stage, then one more hundredth of a foot produced 0.03 cfs. `get_anomalies`
divided a 0.0 by a 30-day mean of 0.826 cfs, called it "100% deviation", and
that number became "severe drought conditions emerging" in a published record.
A percentage against a floor reading describes the instrument, not the water.

**A twice-daily agent sampling a once-daily cycle.** Plotting 72 hours of
stage shows one peak per day — maximum before dawn, minimum in the afternoon,
total range 0.11 ft:

```
Sep 8   peak 2.12 @ 05:45   trough 2.08 @ 15:00
Sep 9   peak 2.13 @ 01:45   trough 2.07 @ 16:45
Sep 10  peak 2.14 @ 05:45   trough 2.03 @ ~20:00
```

The single daily peak and the 0.11 ft amplitude rule out tide, which in this
reach would be mixed semidiurnal with two peaks a day and a range in feet.
Beyond that the cause is not established. An afternoon minimum fits
evapotranspiration; it fits a daily irrigation withdrawal equally well in an
agricultural valley, and a stage series cannot separate them. The troughs
deepening while the peaks hold does track the 98.6°F heat wave the Weather
agent recorded on the 9th, which is suggestive of ET and still not proof.

The first version of this fix asserted evapotranspiration in the tool output
and the system prompt — agent-facing text, which the agent would have
repeated as fact in published records. That is the same unearned assertion
this file exists to catalogue, committed while fixing one. The shipped
version states the measured cycle, states that tide is ruled out, and
explicitly instructs the agent not to name a cause. The fix never depended on
knowing one: what matters is that the cycle exists and the agent was sampling
it blind.

The River agent runs at 00:00 and 12:00 Pacific — near the peak and near the
trough — and compared each run against its own previous observation. Every
consecutive pair therefore straddles opposite phases: peak-to-trough reads as
collapse, trough-to-peak reads as recovery, forever, and neither is a trend.
It passed unnoticed for weeks only because the absolute numbers were small
enough that the swing looked like noise. Once the trough crossed the rating
floor, the same comparison produced a 100% change.

`River/hydrology.py` now holds both facts once. Floor-pinned discharge is
labelled and never scored — `get_anomalies` returns those readings in a
separate `at_rating_floor` list with a note saying why they carry no
percentage. And `get_station_summary` attaches a `daily_cycle` block to each
latest reading: the day's min and max, where this reading sits between them,
and the reading from the same point in yesterday's cycle, which is the only
like-for-like comparison available. A collector gap that removes the
same-phase reading is stated as such rather than silently falling back to the
nearest available point.

Note what the record got *right*, because the fix should not erase it: flow at
Near Napa really has reached zero in the afternoons, and St Helena has been
dry for weeks. The decline is real. What was wrong was the direction of the
current move, the magnitude, and the language.

This is the same correction as wind units, hotspot currency, AQI clocks and
USGS qualifiers, now applied to a comparison rather than a value: the frame
has to travel with the number. A reading needs its unit, its clock, its
provenance — and, at a station that breathes once a day, its phase.

### A rule that could not be silent (2026-09-11)

The first reading of the shadow verdicts produced one finding about the model
and one about the rules, and the second was the useful one.

Coverage first, because it nearly derailed the reading. `shadow_report.py`
showed 176 of 227 runs with no verdict and I took that for the rules failing
three runs in four. They were not: node-01's shadow columns went live on
2026-09-04 — Weather 08:00, AQI 09:00, Fire 10:00, one deployment landing in
each domain's next scheduled run — and every unscored row predates it. A
week-old instrument, read through a 30-day window, looks broken. The report
now names a deployment boundary as one.

On the model, Fire's result is clean: all four rules-only disagreements are
the persistence exception, stated verbatim in each summary. Net of them the
rules and the model agree nine times out of nine.

On the rules, the result is not clean. `new_hotspot_since_last_run` fired on
13 of 13 scored runs. `frp_rising` on 12 of 13, and what it fired on was:

```
frp_rising: 0.09 -> 0.10 MW at 43.3mi     (+11%,  a hundredth of a megawatt)
frp_rising: 9.19 -> 9.25 MW at 31.1mi     (+0.7%, six hundredths)
```

Consecutive VIIRS retrievals of one pixel differ by more than that for view
angle, atmospheric correction, and which of three satellites made the pass.
The rule had no minimum magnitude and no floor on the resulting value, so it
was reading the instrument. Two of six rules were effectively constants, and
enforcing them would have made `must_flag` true on every Fire run — the ⚠️
that fired on every USGS reading, arrived at independently by a different
route.

`frp_rising` now needs both a proportional rise (`FRP_RISE_MIN_FACTOR`, 1.25)
and a resulting value at or above `FRP_NOTABLE_PERCENTILE` of this
collector's own history. Gating on the *new value* rather than the delta is
deliberate: a fire doubling from 0.1 to 0.2 MW has doubled and still does not
matter. Reusing the existing percentile matters too — it is a threshold the
codebase already argued for, rather than a magnitude invented to fit two data
points. The factor is a judgement and says so, calibrated to clear both
observed false positives with room to spare.

The percentile arithmetic moved into `thresholds.py`. It had been in
`mcp_server.py`, the rule was about to hold a second copy, and two copies of
an index calculation is how the flag thresholds drifted before that module
existed — the tool and the rule would have called different readings notable
while reading the same column.

`new_hotspot_since_last_run` is untouched and still fires on essentially
every run. FIRMS returns new detections continuously, so "new since last run"
may be a true statement that carries no information. That one needs deciding
rather than tuning.

The generalisation: a rule that cannot be silent cannot be an authority. The
argument for enforcing deterministic rules over a model assumes the rules
discriminate, and the only way to know whether they do is to run them in
shadow and count. This is the first finding in this project that came from
the system's own instrumentation rather than from a human reading a record —
and it says the instrument was measuring the wrong thing.

### 60,000 tokens to say the river is low (2026-09-11)

River's agent ran at 116,437 and 81,881 input tokens. Weather ran at 16,658.
Synthesis, reasoning across four domains, ran at 15,090. The cheapest
reasoning in the system was costing five to seven times the most complex.

`get_readings_since(48h)` was the reason: 48 hours at 15-minute polling, two
stations, two parameters, serialised as 768 JSON rows — measured at 65,925
tokens. The agent's job with that was to conclude the river is low.

Two per-row repetitions accounted for much of it, and the second one is
instructive because it was mine and it was four days old. `qualifier_meaning`
was added on 2026-09-07 so the agent would stop guessing what `P` meant; it
then restated "P (provisional, subject to revision)" on every row, in a table
where 28,788 of 28,788 readings are P. Roughly 15% of the prompt was one
sentence. It is now a legend emitted once, with the gloss kept inline only
for NOTABLE codes — Ice on one row in a hundred is exactly what the
annotation is for, and a reader should not have to cross-reference to see it.

Then, having fixed that, the first version of the replacement tool attached a
140-character floor note to every hourly row whose minimum was 0.0 — which is
most of them. The same bug, reintroduced within the same hour by the same
reasoning. It is now a boolean flag plus one explanation at the container
level.

`get_hourly_series` replaces the raw dump in `gather_context`: one row per
hour per parameter with min, max, reading count and the distinct qualifiers
seen. Hourly min and max rather than a mean because the minimum is the part
that matters here — the daily low is what crosses the rating floor, and a
mean would hide it. Measured at 80% smaller than the raw series, taking
River's whole context down about 55%.

The detail was not buying anything. Consecutive 15-minute readings at this
station differ by a hundredth of a foot or not at all, and since
`get_station_summary` began returning the daily cycle — min, max, position in
the day's range, same-phase reading from yesterday — the raw series was
redundant as well as expensive. The run that produced "severe drought
conditions emerging" was reading that dump.

Then the whitespace. `json.dumps(indent=2)` was spending about 30% of every
tool payload on spaces and newlines — a model reads compact JSON just as
well. All 25 payloads across the four domain servers now go through
`agent_runtime.compact_json`. The offline tools keep their indentation
deliberately: `extract_training_data.py` and the reports are read by people.

Measured on a reconstruction of node-01's data, River's whole context:

```
raw series, pretty-printed    ~96,480 tokens
hourly series, pretty         ~43,445    (the aggregate alone, -55%)
hourly series, compact        ~34,510    (both, -64%)
```

Fixing the packaging turned up a third instance of an old failure.
`agent_runtime.py` was added at the repo root on 2026-09-10 and imported by
all four agents, and the domain `Dockerfile` never copied it — so the
container build has been broken since that merge, reproduced here as
`ModuleNotFoundError: No module named 'agent_runtime'`. It does not bite on
node-01, which runs from the repo rather than the image, which is exactly why
nobody noticed. `publishers.json` missed the Synthesis image twice before
this. The compounding detail is worth keeping: the missing module is the one
holding `record_failed_run`, so the failure caused by its absence is also the
failure that cannot be recorded.

The generalisation is uncomfortable and worth stating plainly: every fix that
gives a model context costs tokens, and the cheap way to give context is
per-row while the right way is usually once. Both instances here were
introduced while correcting a genuine defect, by someone who had just
finished explaining why the defect mattered.

### The system found one (2026-09-12)

The 2026-09-12 18:00 synthesis record contains this, unprompted:

> Both records report hotspotCount: 0 in the structured field despite the
> summary describing "9-10 hotspots in range matching the confirmed incident"
> — this is a field/summary conflict worth noting: the numeric hotspotCount
> field says zero while the prose describes nearly a dozen matched
> detections. I'm reporting this discrepancy rather than picking a side.

That is the two-authors rule doing precisely what it was written for four
days earlier, and it is the first defect in this project found by the system
rather than by a person reading a record. The shadow rules could not have
caught it — they read the same table. The publisher could not — it wrote the
wrong number confidently. What caught it was a model noticing that two
descriptions of one event disagreed and declining to resolve them.

The bug was real, and it was the same bug as everything else here. Three
things described one fire block, from three different populations:

```
nearestHotspot* / maxHotspot*   collected_at >= observedAt - 72h, distance NOT NULL
get_nearest_hotspots (prose)    collected_at >= now - 72h, ordered by distance
hotspotCount                    |collected_at - observedAt| < 6h, no distance filter
```

Different window, different clock, no distance bound. And `collected_at`
freezes at first-seen time under the INSERT OR IGNORE dedup — the collector
says so in its own comments — so the count measured how many detections were
first STORED near observedAt, not how many hotspots existed. VIIRS gives
about six overpasses a day across three satellites and NRT lags roughly three
hours, so detections arrive in bursts. Land between bursts, as a steady
75%-contained fire does, and the count reads 0 with ten hotspots current.

`hotspotCount` now draws from the same population as every other structured
fire number, `HOTSPOT_COUNT_WINDOW_HOURS` is retired, and the lexicon says
what the field means rather than what its window was. A count of 0 now agrees
with an absent `nearestHotspotDistanceMi` instead of contradicting it.

Worth recording what this says about the domain-agent question that had been
running all week. The value of a model here was not in the flag, which the
rules reproduce, nor in the prose, which a template could assemble. It was in
holding two accounts of the same event side by side and noticing they did not
match — and then saying so instead of choosing. That is not arithmetic over
one table, and it is the one thing in this system that no rule was ever going
to do.

### The control arm (2026-09-12)

The week's open question — do the domain agents earn a model — had no
instrument. The shadow verdicts compare one boolean, and the boolean is the
part where the rules and the model agree by construction, since flag_rules.py
encodes the criteria the prompt states.

`River/template_summary.py` is the control: the same summary, from the same
database, with no model. It is a pure function of (conn, observed_at) and
reads nothing the agent wrote, so `river_template_report.py` can run it
retroactively over every observation already recorded rather than waiting a
fortnight to accumulate a sample.

The template's honest limitation is itself a result. It cannot flag on
conditions, because River has no numeric flag criteria —
`floodStageThresholdFt` is unconfigured for both Napa gauges, which is why
River has no `flag_rules.py` at all. So it flags only on collector gaps and
says so in its output. Every condition flag the agent has ever raised on
River is therefore judgement with no arithmetic behind it. That is not
automatically wrong; it is the thing the comparison exists to price.

The sharper half is NUMERIC TRACEABILITY. Every figure in a summary is
extracted and checked against values the database can produce at that
instant — readings, seven-day statistics, diel min/max, same-phase deltas.
Against the real 2026-09-10 summary it reports `0.826` and `100`, which are
the fabricated 30-day baseline and the percentage computed against the rating
floor. It does not report `0.32`, `0.03`, `2.11`, `2.07` or `0.47`, which are
real readings. The template's own summary of the same instant reports
nothing.

UNVERIFIED means "no value in the database matched", not "false". A
legitimately derived figure the checker cannot recompute lands in the same
bucket — `0.295`, the agent's seven-day mean, does exactly that against a
fixture whose mean differs.

Building the checker reproduced the failure it was built to detect, twice.
The first version dropped every number below 3.0 as trivial, which in a river
running at hundredths of a cubic foot per second is almost every real value —
it silently swallowed the fabricated baseline. The distinction that actually
holds is decimals versus integers: readings carry decimals, window sizes do
not. Then that rule swallowed "100% deviation", because a percentage is an
integer too; an integer followed by a percent sign is a claim, not furniture.
Both errors were mine, both were caught by a test asserting the known-bad
2026-09-10 summary must fail, and neither would have been visible without it.

### What generalises, and what doesn't

The tempting conclusion is that models can't handle deterministic rules. That's
too broad, and it points at the wrong fixes. Ask the model once, cold, whether
93.7% is below 25% and it answers correctly every time.

The 2026-09-06 record makes the narrower point better than any argument.
Three values in a single run, same model, same prompt:

  Diablo onset countdown   11 days -> 10 days   correct, computed by seasonal_context()
  "Day N of drought"       143+ -> 143+ -> 145+ frozen a day, then jumped two
  "Nth overnight cycle"    71st -> 72nd -> 73rd increments once per run, not per event

The countdown is right because code calculates it from the date. The other two
are carried in the agent's own prose from run to run, and nothing anchors them.
Same run also restated 34.2% humidity as "below fire-weather thresholds" — its
own threshold is 25% — and described winds at 190-280° as "offshore/Diablo
direction", when the prompt defines Diablo as NE/E and calls SW/W the marine
direction. Both errors move in the direction that supports the standing
conclusion.

So the failure isn't rules. It's state that has to be re-narrated to be used,
inside a loop with nothing outside it to check against. Three loops here read
what the agents themselves wrote: domain agent memory, synthesis memory, and
the ledger — and the ledger was the one designed to be the anchor.

The stronger form of the lesson: a grounding mechanism that isn't causally
independent of the thing it grounds is a mirror. Confirming one model's claim
with another model's judgement isn't verification, especially when both share a
prompt lineage and a model family. That also bears on the multi-model idea in
"Possible additions" — two models agreeing is weak evidence if they share
priors, and independence is most of what a second opinion is worth.

Worth noting what this cost to find: the drift came with rising confidence and
a perfect self-reported track record. At the point the system was least
reliable, its own scoreboard read 128 for 128. "Is the system reporting
problems?" was not a usable check, and it took reading a published record and
noticing a counter hadn't incremented.

### Answered: the Weather agent's constant flagging (2026-09-08)

The open question here was whether it was model drift or a correctly applied
but badly calibrated threshold. It was neither, and the framing was wrong —
both options assumed the data was right.

`Weather/collector.py` read the NWS `windSpeed` value and ignored its
`unitCode`. NWS reports wind on the observations endpoint in km/h
(`wmoUnit:km_h-1`); the collector assumed m/s and converted m/s → km/h → mph.
Every wind value in the database was 3.6x too high. A 19.6 mph gust was stored
as 70.5 mph, and the 45 mph gust rule was crossed by any true gust above
12.5 mph — a light breeze, most days. The threshold was fine. The model was
reasoning correctly about numbers that were already wrong when it saw them.

The thing worth keeping from this: nothing in the design could have caught it.
`flag_rules.py` reads the same column as the model, so the shadow verdict
agreed with the model on every run — two checks that look independent, sharing
one corrupted input. That is the same shape as the prediction ledger being a
mirror, one layer further upstream. Agreement between two things is only
evidence if they don't share a source. Synthesis compounded it: `_confirms`
resolves fire predictions against the published `windGustMph`, so the ledger
purged on 2026-09-06 had been refilling with confirmations from phantom wind.

It surfaced from a physical-plausibility read, not from any instrumentation:
sustained 74.6 mph with 95.3 mph gusts at Napa County Airport is Category 1
hurricane force, and it was appearing in routine September summaries.
Worth considering a sanity bound on collector inputs — a value outside what
the location can physically produce is a collector bug, not an observation.

Also unresolved: `domainsObserved` is still hardcoded to all four domains in
`Synthesis/publisher.py`, and domain records still publish `flagReason` as an
empty string. Both are the same shape as the `agentModel` problem — a field
asserting something nobody checked — and both are waiting on the same
divergence data, since `rules_fired` is the natural source for a real
flagReason.

## Architecture decisions made

**Why separate venvs per stack?** Independence — each stack can be updated,
restarted, or replaced without affecting the others.

**Why MCP servers as stdio subprocesses?** Simplicity at this scale. Each
agent spawns the MCP server per tool call rather than running it persistently.
Switching to persistent HTTP is one flag (`--http`) when needed.

**Why does Synthesis use ATProto instead of reading SQLite directly?**
The Synthesis agent runs on a separate machine from the Pi nodes — it has no access
to their local SQLite databases. ATProto is the message bus: domain agents publish
structured observations as lexicon records, Synthesis subscribes via
`com.atproto.repo.listRecords` (fetch mode, cron-triggered), and reasons across
whatever it finds. This also decouples the agents completely — a node can be replaced,
moved, or added without any change to Synthesis. The subscriber verifies author DIDs
against `publishers.json` (interim trust registry) before accepting records.

**Why a self-hosted PDS instead of publishing to `bsky.social`?**
The original motivation was Bluesky (a place to see posts); the actual interest is
ATProto — portable identity, federation, data bound to the DID rather than the
platform. Staying on `bsky.social` meant the node's identity was, in practice,
Bluesky-the-company's to revoke or rate-limit. Running the official PDS on the Pi
means the node's identity depends only on the protocol, not on a specific operator's
infrastructure — closer to the "A Half-Built Garden" framing than borrowing Bluesky's
implicit trust layer ever was. It also cleanly separates concerns: domain agents write
structured records for other agents to consume (no `app.bsky.feed.post` needed);
only Synthesis, which has an actual human audience, still touches the public network.

**Why register a separate domain (`watershed-agent.dev`) instead of using the existing `cpricedomain.net`?**
Cloudflare Tunnel's cert-issuance flow (`cloudflared tunnel login`) requires a zone
already on Cloudflare's nameservers — there's no way to delegate just one subdomain's
NS records without moving the whole zone, and DNSimple (where `cpricedomain.net`
lives) doesn't support that either. Rather than touch a domain other things depend
on, registering a small dedicated domain directly through Cloudflare Registrar meant
DNS was authoritative on Cloudflare from the moment of registration — no delegation,
no propagation wait — and it separates "Agentic Watershed infrastructure identity"
from the personal domain, which also fits the project's own theme.

**Why Cloudflare Tunnel instead of port-forwarding on the home router?**
Residential ISPs increasingly put connections behind CGNAT, which makes inbound
port-forwarding impossible regardless of router configuration — and even where it
works, it means running a public TLS endpoint on a home network with no DDoS
protection and an IP that can't be rotated without breaking the DID's service
endpoint. A tunnel makes only an outbound connection from the Pi; no inbound ports
are ever opened, it works regardless of CGNAT, and Cloudflare terminates TLS.

**Why Sonnet for Synthesis, Haiku for domain agents?**
Cross-domain reasoning across multiple observation sets warrants more capability.
Domain agents do single-domain threshold assessment — Haiku is sufficient and cheaper.

**Why are agents cron-triggered rather than long-running?**
Simpler, more robust, easier to debug. A failed run doesn't affect the next one.
Statelessness is a feature — memory is explicit via the observations tables.
