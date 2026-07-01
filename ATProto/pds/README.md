# Self-hosted PDS (Pi)

Runs the official [bluesky-social/pds](https://github.com/bluesky-social/pds)
so domain agent nodes have their own ATProto identity instead of borrowing
`bsky.social`. Domain agents write structured lexicon records here; the
public Bluesky app layer (and the human-facing advisory) stays Synthesis's
job, not this PDS's.

## One-time setup on the Pi

1. **DNS + TLS**: point a subdomain (e.g. `pds.yourdomain.example`) at the
   Pi's public IP. The PDS needs to be reachable at that hostname over
   HTTPS — did:plc documents record the PDS's service endpoint, and other
   agents (Synthesis) resolve through it.

2. **Generate secrets**:
   ```bash
   openssl rand --hex 16   # PDS_JWT_SECRET
   openssl rand --hex 16   # PDS_ADMIN_PASSWORD
   ```
   For the PLC rotation key, use the official install script's keygen step
   (see the bluesky-social/pds README) rather than hand-rolling one — it
   needs to be a valid secp256k1 private key in the format the PDS expects.

3. **Copy and fill in env**:
   ```bash
   cp pds.env.example pds.env
   # edit pds.env with the generated secrets and your hostname
   ```

4. **Create the data directory** referenced in `docker-compose.yml`
   (`/home/cprice/pds-data` by default — adjust to match your Pi's layout):
   ```bash
   mkdir -p /home/cprice/pds-data
   ```

5. **Start it**:
   ```bash
   docker compose up -d
   ```

6. **Create the node's account** (mints its did:plc):
   ```bash
   docker exec -it pds node dist/scripts/create-account.js \
     --email you@example.com \
     --handle napa-node-01.pds.yourdomain.example \
     --password <account-password>
   ```
   Save the returned DID — that's what goes into `node_config.json` and
   into Synthesis's trusted-publishers list.

## Persistence

Everything durable — accounts, repo records, blob store, PLC rotation key —
lives under `/pds` inside the container, bind-mounted to the host path in
`docker-compose.yml`. Recreating the container without that mount destroys
the node's entire identity and history. Back up that directory like you
would any other credential store, not like a cache.

## After this is running

- `ATProto/publisher.py`: point `BSKY_PDS` at `https://pds.yourdomain.example`
  instead of `https://bsky.social`, and use the node account's handle/password
  instead of the `napanode1.bsky.social` app password.
- `Synthesis/agent/agent_atproto.py`: point record reads
  (`com.atproto.repo.listRecords`) at the same PDS URL, and update the
  trusted-DID registry to the new did:plc.
