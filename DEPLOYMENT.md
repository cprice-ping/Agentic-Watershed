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

## Migrating node-01 to cheaper hardware (e.g. a Raspberry Pi Zero 2 W)

This is a different migration than the one above — same node, same identity,
new physical hardware, and (deliberately) **not** a move to docker-compose.
The reasoning: Docker's own daemon overhead is negligible on a Pi 4/5-class
device but a real tax on something as RAM-constrained as a Zero 2 W (512MB) —
exactly the device this migration exists to make cheaper. Stick with the
native venv+cron setup already running on node-01 today; don't containerize
as part of this move.

**What has to move, and why one piece is different from the rest:**

- The four domain SQLite DBs (`River/data/watershed.db`, `Weather/data/weather.db`,
  `AQI/data/aqi.db`, `Fire/data/fire.db`) and `ATProto/data/publisher.db` —
  just files, copy them.
- `node_config.json` and `.env` — also just files, copy them (same values,
  nothing node-specific needs to change since this is the same node moving,
  not a new one).
- **The PDS's data directory (`/home/cprice/pds-data` by default, per
  `ATProto/pds/README.md`) is identity-critical, not just data.** It holds
  the account, repo records, blob store, and — critically — the PLC rotation
  key. That key is what makes this node's DID (`did:plc:ggztd5hjk3cnkhgzdk4rmqan`)
  *this* node's identity. Losing it doesn't just lose history, it loses the
  ability to prove you're still the same node at all. Treat this directory
  like a credential store during the move, not like a cache — verify the
  copy, don't just trust `rsync` exited 0.
- The Cloudflare Tunnel credentials (`/etc/cloudflared/` — note: root's
  systemd service reads from there, not `~/.cloudflared`, a previously
  hard-won detail worth not re-learning the hard way) — either move the
  existing tunnel's credentials file to the new device, or re-run
  `cloudflared tunnel login` and re-route DNS if starting the tunnel fresh
  there.

### Phase 0 — validate before committing anything real

Don't migrate real data onto unproven hardware. Get a Zero 2 W, and before
touching any production identity or history:

1. Flash it, get it on the network, confirm `docker --version`-equivalent
   readiness isn't needed (we're not using Docker here) — just Python 3.11+
   and enough headroom to run the PDS's Node.js process.
2. Stand up a **throwaway** PDS instance on it (new account, new DID, per
   `ATProto/pds/README.md` — not the real node-01 identity yet).
3. With that throwaway PDS running, manually trigger a domain collector and
   agent run concurrently (e.g. `python collector.py` in one shell while
   `python agent.py` runs in another) and watch `free -h` under that combined
   load — this is the realistic worst case (a collector firing while an
   agent's mid-reasoning-call, or the PDS mid-request from Synthesis's next
   fetch).
4. If that's stable with reasonable headroom, proceed. If it's tight or
   swaps, that's the answer to "is the Zero 2 W actually enough" — cheaper
   to learn that here than after moving real identity onto it.

### Phase 1 — prep the new device

1. Same OS family (Raspberry Pi OS), Python 3.11+, per-stack venvs exactly
   as documented in each stack's own README (`River/README.md`, etc.) —
   nothing about venv setup changes based on which Pi model this is.
2. `git clone` this repo; don't copy the old Pi's checkout wholesale (avoids
   dragging over anything stale — logs, `__pycache__`, etc.).

### Phase 2 — migrate the PDS (the identity-critical step)

1. On node-01 (the Pi 5): `docker compose -f ATProto/pds/docker-compose.yml stop`
   — stop writes before copying, same principle as backing up a running
   database.
2. Copy `/home/cprice/pds-data` to the new device (`rsync -av`, verify file
   counts/sizes match on both ends — don't just check the exit code).
3. Copy `ATProto/pds/pds.env` (the real one, with generated secrets — not
   `pds.env.example`) to the new device, same path layout.
4. On the new device: `docker compose -f ATProto/pds/docker-compose.yml up -d`,
   confirm it starts clean (`docker compose logs -f pds`).
5. **Don't start the old Pi 5's PDS back up while testing the new one** —
   two instances both claiming the same DID's data at once is exactly the
   kind of split-brain that corrupts identity state. Keep the Pi 5's PDS
   stopped until the new device is confirmed working end-to-end.

### Phase 3 — migrate the domain stacks and cron

1. Copy the five DBs listed above into the new checkout's matching
   `<Stack>/data/` paths.
2. Copy `node_config.json` and `.env` as-is (same node, same values).
3. **Test one service manually before touching cron** — same principle as
   the docker-compose migration above:
   ```bash
   cd River && .venv/bin/python collector.py
   sqlite3 data/watershed.db "SELECT * FROM readings ORDER BY collected_at DESC LIMIT 3;"
   ```
   Repeat for `weather`, `aqi`, `fire`.
4. Move the Cloudflare Tunnel credentials (`/etc/cloudflared/`) to the new
   device, or re-create the tunnel and re-route DNS if that's cleaner —
   either way, confirm `https://napa-node-01.watershed-agent.dev/xrpc/_health`
   resolves through the tunnel from the new device before trusting it.
5. Add the same cron lines from `README.md`'s legacy (non-Docker) block to
   the new device — unchanged, since this is native venv+cron on both ends.

### Phase 4 — cutover with a real fallback window

1. Leave the Pi 5's cron **disabled** (comment it out, don't delete it) once
   the new device's cron is live — two nodes publishing under the same DID
   concurrently would corrupt the PDS's repo state the same way two running
   PDS instances would.
2. Watch the new device's logs through a few full cron cycles (collectors,
   agents, publisher) before considering this done.
3. Confirm Synthesis still picks up records normally — nothing about
   `Synthesis/publishers.json` needs to change, since the DID didn't change,
   only which physical device is behind it.
4. Once confident: decommission the Pi 5's production role (stop its PDS
   container permanently, remove its cron lines for real) and repurpose it
   as the dev/experimentation box — the MLX fine-tuning work, local model
   pilots, whatever comes next. It already has everything installed for that.

## Deploying a code change to node-01 (venv + cron)

node-01 still runs from a git checkout with per-stack venvs and cron, not
from the containers above. Most merges need only a pull; the rest of this
section is for the cases that need more.

```bash
cd /home/cprice/Agentic-Watershed && git pull
```

That is the whole deploy for a change to an agent prompt, an MCP tool, a
collector's parsing, or the publisher. Cron picks it up on the next run.

Check what is actually running rather than assuming the pull took. Every
agent logs its commit at startup:

```bash
grep "Code version" */logs/agent.log | tail -4
```

This exists because a pull once reported "Already up to date" while the
checkout sat on a detached commit, and Fire ran four weeks of stale code
before anyone noticed.

### When a change adds a new collector or a new table

Two extra steps, in this order.

Create the schema once, before cron can race it:

```bash
cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python incidents_collector.py --init
```

Then run it once by hand and read the output. This is the real check that
parsing holds against the live API rather than against a payload someone
pasted into a review:

```bash
cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python incidents_collector.py
```

Counts of zero, an empty nearest list, or every numeric field showing `None`
mean the field mapping is wrong. Collectors that store a raw JSON column can
be corrected after the fact without re-polling; those that do not cannot, so
check this before walking away.

Finally add the cron line. Every collector's own module docstring carries the
line to use and the reasoning for its cadence — copy it from there rather
than inventing a schedule, because the cadence is usually tied to when
something downstream reads the data:

```bash
crontab -e
```

```cron
50 2,14 * * * . /etc/environment && cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/python incidents_collector.py >> logs/incidents.log 2>&1
```

`. /etc/environment &&` is not optional. Cron does not source it, so without
that prefix every API key and credential is missing and the collector fails
on its first request. The full current crontab is in `CONTEXT.md` under
"Cron (current)".

### When a change adds a migration

Migrations run from the collector's `init_db` on every startup and are
guarded by the `schema_migrations` table, so they apply on the next
scheduled run with no manual step. To apply one immediately rather than
waiting for cron:

```bash
cd /home/cprice/Agentic-Watershed/Weather && .venv/bin/python collector.py --init
```

Watch the log for the migration's own line — the wind unit correction, for
instance, logs `Corrected N historical observation rows`. Seeing it once is
the confirmation; seeing it twice means the guard is broken and the data has
been double-corrected.

### When a change adds a Python dependency

Each stack has its own venv and they are independent:

```bash
cd /home/cprice/Agentic-Watershed/Fire && .venv/bin/pip install -r requirements.txt
```

Note that `sqlite3` is in the standard library, so ad-hoc DB queries need
no venv at all — only outbound HTTP (`httpx`) and the Anthropic SDK do.

### Editing the crontab without taking the node down

Every cron line begins `. /etc/environment && cd ... && .venv/bin/python ...`.
Three properties of that prefix are load-bearing and none of them are
obvious. All three were established by breaking them on 2026-09-09, which
stopped every collector and agent on node-01 with no error anywhere.

**The leading `. ` is not decoration.** Without it the line reads
`/etc/environment && cd ...`, which asks the shell to *execute* a 644 data
file. It fails, and `&&` short-circuits the rest, so the job silently never
runs. There is no log line, no mail, no non-zero exit anyone sees — the node
just stops producing records.

**`/etc/environment` must carry `export` on every line.** Cron runs jobs
under `/bin/sh`, which is dash, and sourcing a file of plain `KEY=value`
pairs sets shell variables that are never inherited by the python process:

```bash
$ /bin/sh -c '. ./env && python3 -c "import os;print(os.environ.get(\"K\"))"'
None     # file contained  K=v
v        # file contained  export K=v
```

This is why the keys live there with `export` rather than as bare
assignments, and why they must not be moved to a crontab-level
`KEY=value` block "for tidiness" — cron exports those itself, so both work,
but only one of them survives being read from a file.

**`/etc/environment` must stay readable by the cron user.** It holds API
keys, so 644 is too open; but `chmod 600` locks out `cprice`, whose cron jobs
source it, reproducing the same silent short-circuit. Use group read:

```bash
sudo chown root:cprice /etc/environment && sudo chmod 640 /etc/environment
```

Edit the crontab as a file rather than with `crontab -e`, which needs a
terminal that can host a full-screen editor and hangs where one is not
available:

```bash
crontab -l > ~/cron.bak
cp ~/cron.bak ~/cron.new     # edit ~/cron.new
diff ~/cron.bak ~/cron.new   # read this before installing
crontab ~/cron.new && crontab -l | grep -v '^\s*#'
crontab ~/cron.bak           # revert
```

Then confirm a job actually ran rather than trusting the text. River is the
fastest signal at 15-minute intervals:

```bash
date; tail -3 /home/cprice/Agentic-Watershed/River/logs/collector.log
```

To test a line the way cron will run it — dash, sourcing, no interactive
shell — rather than the way your login shell would:

```bash
/bin/sh -c '. /etc/environment && cd /home/cprice/Agentic-Watershed/River && .venv/bin/python collector.py' && echo OK
```

## Why `node_config.json` is committed to git (and what that means)

It's tracked so the *default* node (napa-node-01) works out of the box
without a manual setup step for anyone reading the repo. For a second node,
don't edit the tracked copy in place — either maintain node-02's config in
a separate clone/directory (the normal case — a real second node is a
separate checkout on separate hardware), or if running multiple nodes from
one checkout for local testing, override the mount:
`docker compose run --rm -v $(pwd)/node-02.config.json:/app/node_config.json:ro river ...`

## GitHub Actions deploy — one-time Azure setup

`.github/workflows/deploy-synthesis.yml` rebuilds the Synthesis image and
updates its Container Apps Job whenever `Synthesis/**` changes on `main`.

Synthesis runs from a container image, so a merge changes nothing until the
image is rebuilt — `subscriber.py`, `publisher.py` and `agent/` are all
`COPY`'d in at build time. That gap is why #47's subscriber fix reached the
repo well before it reached Azure, and it's what this closes.

Auth is **OIDC**: GitHub presents a short-lived token, Azure trusts it for
this repository only. No standing Azure credential in the repo.

### 1. App registration, service principal, federated credential

Safe to re-run. Every step checks for what it needs before creating it —
`az ad app create` does **not** fail on a duplicate display name, it silently
creates a second app registration with a different `appId`, which is how you
end up with `AZURE_CLIENT_ID` naming one app while the role assignment sits on
another's service principal.

```bash
APP_NAME=gha-agentic-watershed
RG=rg-agentic-watershed
REPO=cprice-ping/Agentic-Watershed

# --- app registration -----------------------------------------------------
COUNT=$(az ad app list --display-name "$APP_NAME" --query "length([])" -o tsv)
if [ "$COUNT" -gt 1 ]; then
  echo "WARNING: $COUNT apps named $APP_NAME — an earlier run created duplicates."
  az ad app list --display-name "$APP_NAME" \
    --query "[].{appId:appId, created:createdDateTime}" -o table
  echo "Pick the one matching AZURE_CLIENT_ID and set APP_ID by hand, then skip ahead."
fi

APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)
  echo "created app $APP_ID"
else
  echo "reusing app $APP_ID"
fi

# --- service principal ----------------------------------------------------
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null)
if [ -z "$SP_ID" ]; then
  SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
  echo "created service principal $SP_ID"
else
  echo "reusing service principal $SP_ID"
fi

# --- federated credential -------------------------------------------------
WANT="repo:${REPO}:ref:refs/heads/main"
HAVE=$(az ad app federated-credential list --id "$APP_ID" \
        --query "[?name=='main'] | [0].subject" -o tsv 2>/dev/null)

if [ -z "$HAVE" ]; then
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"main\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"${WANT}\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
  echo "created federated credential"
elif [ "$HAVE" != "$WANT" ]; then
  echo "MISMATCH: credential 'main' has subject $HAVE, expected $WANT"
  echo "Fix with: az ad app federated-credential update --id $APP_ID --federated-credential-id main --parameters '{\"subject\":\"${WANT}\"}'"
else
  echo "federated credential already correct"
fi
```

`subject` is the security boundary — it pins trust to this repo and this
branch. A second credential with `subject: repo:<repo>:environment:<name>` is
what you'd add later if you want an approval gate.

Manual `workflow_dispatch` runs from `main` are covered by the credential
above. Dispatching from another branch needs its own credential.

### 2. Role assignment

Also safe to re-run.

```bash
SUB=$(az account show --query id -o tsv)
SCOPE="/subscriptions/$SUB/resourceGroups/$RG"

EXISTING=$(az role assignment list --assignee "$APP_ID" --scope "$SCOPE" \
            --query "[?roleDefinitionName=='Contributor'] | length([])" -o tsv)

if [ "$EXISTING" = "0" ] || [ -z "$EXISTING" ]; then
  az role assignment create \
    --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
    --role Contributor --scope "$SCOPE"
  echo "role assigned — allow a minute or two for it to propagate"
else
  echo "role assignment already present"
fi

az role assignment list --assignee "$APP_ID" --all -o table
```

`--assignee-object-id` with an explicit principal type is used rather than
`--assignee`, which has to resolve the id through the directory and fails
intermittently against a service principal created moments earlier.

Contributor on the resource group is the simple option and is broader than
strictly needed — it covers both `az acr build` (which schedules an ACR Task,
so `AcrPush` alone is insufficient) and the Container Apps Job update. A
narrower setup is Contributor scoped to the registry plus a Container Apps
role scoped to the job.

### 2b. Verify before touching GitHub

These three values must match the repository secrets exactly. Run this and
compare:

```bash
echo "AZURE_CLIENT_ID       = $APP_ID"
echo "AZURE_TENANT_ID       = $(az account show --query tenantId -o tsv)"
echo "AZURE_SUBSCRIPTION_ID = $(az account show --query id -o tsv)"
echo
echo "federated subject: $(az ad app federated-credential list --id "$APP_ID" --query "[?name=='main'] | [0].subject" -o tsv)"
echo "role assignments:"
az role assignment list --assignee "$APP_ID" --all \
  --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

A subscription with more than one entry is worth checking twice: if the
resource group lives in a different subscription than the one
`az account show` returns, the workflow fails the same way as having no role
at all.

### 3. Repository secrets

| Secret | Purpose |
|---|---|
| `AZURE_CLIENT_ID` | the `APP_ID` above |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| `ANTHROPIC_API_KEY` | the job's runtime secret |
| `BSKY_SYNTH_HANDLE` | the job's runtime secret |
| `BSKY_SYNTH_APP_PASSWORD` | the job's runtime secret |

The first three are identifiers, not credentials — OIDC means there's no
Azure secret to store. The last three are different: they are the
application's own runtime secrets, and they're needed because `deploy.sh`
re-PUTs the entire job definition (secrets included) even with
`--image-only`.

That is a real expansion of where those secrets live. `az containerapp job
update --image` would avoid it by touching only the image, but `deploy.sh`
deliberately avoids the containerapp CLI extension — see the comment above
its step 6 — because it "mangled secretRef values and stripped volume
mounts", and losing the File Share mount would take `synthesis.db` and the
prediction ledger with it. If you'd rather not hold those three in GitHub,
the alternative is Key Vault plus `azure/get-keyvault-secrets`, which keeps
them in Azure at the cost of another moving part.

### What the workflow does

1. builds and pushes `synthesis:<commit-sha>` via `az acr build`
2. PUTs the job definition pointing at that image
3. re-points `synthesis:latest` at the same build, so a manual
   `./deploy.sh --image-only` doesn't ship an older image
4. reads the job back and **fails** unless the deployed image is the commit
   that just ran — a green tick means the image actually landed

Tagging by SHA also fixes the ambiguity `:latest` created: the job spec now
names the commit it's running, the same way the publisher's `Code version:`
line does on the Pi.

Changes take effect on the job's next scheduled run (`0 6,18 * * *` UTC),
not at deploy time.

### Troubleshooting

**`Error: No subscriptions found for ***.`** — from `azure/login`.

The OIDC exchange *worked*; the token was accepted. What's missing is RBAC:
the service principal has no role assignment, so `az account list` comes back
empty and the login step gives up. A wrong `subject` fails differently
(`AADSTS70021: No matching federated identity record found`), so this error
specifically rules the federated credential out as the cause.

Three things to check, in order:

```bash
# 1. Duplicate app registrations from a re-run of section 1
az ad app list --display-name gha-agentic-watershed \
  --query "[].{appId:appId, created:createdDateTime}" -o table

# 2. Does the app named by AZURE_CLIENT_ID have any role at all?
APP_ID="00000000-0000-0000-0000-000000000000"   # <- your AZURE_CLIENT_ID
az role assignment list --assignee "$APP_ID" --all -o table

# 3. Is the RG in the subscription AZURE_SUBSCRIPTION_ID names?
az group show -n rg-agentic-watershed --query id -o tsv
```

Empty output from (2) is the usual answer — re-run section 2. Allow a minute
or two for propagation, then **Actions → the failed run → Re-run jobs**; no
new commit is needed.

**`FederatedIdentityCredential with name main already exists`** — harmless.
Section 1 now detects this and verifies the existing `subject` instead of
failing.

A failed login costs nothing: the workflow stops before it touches ACR or the
job, so nothing is half-deployed.
