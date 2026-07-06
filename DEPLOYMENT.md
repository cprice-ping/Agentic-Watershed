# Deploying a node

A node is: `docker-compose.yml` (collectors, domain agents, ATProto publisher)
plus `ATProto/pds/docker-compose.yml` (the self-hosted PDS — see
`ATProto/pds/README.md` for that half, it's unchanged by this doc).

Both images built from this repo are generic — nothing node-specific is
baked in. `node_config.json` and `.env` are bind-mounted at runtime, not
copied into the image. **A new node is: clone this repo, write a new
`node_config.json` and `.env`, done.** Rebuilding the image is only needed
when the code itself changes, not when moving to new hardware or a new
location.

## Fresh node setup

1. **Prerequisites**: Docker + Compose plugin (`docker compose version` should
   work), Anthropic API key, AirNow API key.

2. **Set up the PDS first** — see `ATProto/pds/README.md`. You need its DID
   before you can finish `node_config.json` below.

3. **Write `node_config.json`** (repo root, not committed — see below for why
   the current one *is* committed and what that means for you):
   ```json
   {
     "node_id": "napa-node-02",
     "location": "Calistoga, California",
     "did": "did:plc:...",
     "pds_url": "https://napa-node-02.watershed-agent.dev",
     "weather": { "observation_station": "...", "alert_zones": ["..."] },
     "watershed": { "usgs_stations": { "...": "..." } },
     "aqi": { "lat": 0.0, "lon": 0.0, "reporting_area": "..." }
   }
   ```

4. **Write `.env`** from `.env.example` — API keys plus the PDS account
   credentials from step 2.

5. **Build**:
   ```bash
   docker compose build
   ```

6. **Add cron** — see README.md's "Cron schedule" section for the exact
   lines; they call `docker compose run --rm <service> python <script>.py`
   from the repo root instead of `.venv/bin/python`.

7. **Add the new node's DID to `Synthesis/publishers.json`** so Synthesis
   picks up its records — no other Synthesis changes needed (see
   `Synthesis/subscriber.py`'s per-DID PDS resolution, documented in
   `CONTEXT.md`).

## Migrating node-01 from venv+cron to this

Node-01 is currently running via per-stack venvs and host cron, with real
history in `River/data/watershed.db`, `Weather/data/weather.db`,
`AQI/data/aqi.db`, `Fire/data/fire.db`, and `ATProto/data/publisher.db`.
**Don't start fresh** — those databases already exist on disk in exactly the
paths the compose file bind-mounts (`./River/data`, etc.), so switching over
preserves them automatically. No export/import step.

1. `git pull` this branch on the Pi.
2. Copy `.env.example` to `.env` and fill in the same values currently in
   `/etc/environment` (`ANTHROPIC_API_KEY`, `AIRNOW_API_KEY`, `FIRMS_API_KEY`,
   `BSKY_HANDLE`, `BSKY_APP_PASSWORD`, `ATPROTO_PDS_URL`). `FIRMS_API_KEY` is
   new if Fire wasn't running before — register a free `MAP_KEY` at
   https://firms.modaps.eosdis.nasa.gov/api/map_key/.
3. `node_config.json` already exists and is already correct for node-01 —
   nothing to change there (it already has a `"fire"` block if you've pulled
   past the point Fire was added).
4. `docker compose build`
5. **Test one service manually before touching cron**:
   ```bash
   docker compose run --rm river python collector.py
   sqlite3 River/data/watershed.db "SELECT * FROM readings ORDER BY collected_at DESC LIMIT 3;"
   ```
   Confirm it wrote a new row and didn't error. Repeat for `weather`, `aqi`,
   `fire`, and (once there's something to publish) `atproto-publisher`.
6. **Swap the cron lines** — comment out the old `.venv/bin/python` lines,
   uncomment/add the `docker compose run --rm ...` ones (README.md has
   both, clearly marked). Don't delete the old venvs yet.
7. Watch the next few scheduled runs' logs before removing the venvs. If
   anything's wrong, reverting is just switching the cron lines back — the
   venvs and the databases are both still there untouched.

## Why `node_config.json` is committed to git (and what that means)

It's tracked so the *default* node (napa-node-01) works out of the box
without a manual setup step for anyone reading the repo. For a second node,
don't edit the tracked copy in place — either maintain node-02's config in
a separate clone/directory (the normal case — a real second node is a
separate checkout on separate hardware), or if running multiple nodes from
one checkout for local testing, override the mount:
`docker compose run --rm -v $(pwd)/node-02.config.json:/app/node_config.json:ro river ...`
