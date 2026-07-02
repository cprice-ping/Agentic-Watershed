# Self-hosted PDS (Pi)

Runs the official [bluesky-social/pds](https://github.com/bluesky-social/pds)
so domain agent nodes have their own ATProto identity instead of borrowing
`bsky.social`. Domain agents write structured lexicon records here; the
public Bluesky app layer (and the human-facing advisory) stays Synthesis's
job, not this PDS's.

Reachable via a Cloudflare Tunnel — no inbound ports opened on the Pi's
router, works regardless of CGNAT, TLS terminated at Cloudflare's edge.
Domain: `watershed-agent.dev`, registered and DNS-hosted directly on
Cloudflare (Cloudflare Registrar), so no nameserver delegation needed.
Node hostname: `napa-node-01.watershed-agent.dev`.

## One-time setup on the Pi

1. **Install cloudflared**:
   ```bash
   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
   sudo dpkg -i cloudflared.deb
   ```
   (arm64 for a 64-bit Pi OS — use `cloudflared-linux-arm` if running 32-bit.)

2. **Authenticate and create the tunnel**:
   ```bash
   cloudflared tunnel login          # opens a browser link — select watershed-agent.dev
   cloudflared tunnel create napa-pds
   ```
   Note the tunnel ID printed here; it's also in the credentials file path
   written to `~/.cloudflared/`.

3. **Route DNS to the tunnel**:
   ```bash
   cloudflared tunnel route dns napa-pds napa-node-01.watershed-agent.dev
   ```

4. **Configure ingress** — create `~/.cloudflared/config.yml`, then copy it
   (and the credentials file) under `/etc/cloudflared/`, since the systemd
   service runs as root and won't see your user's home directory:
   ```yaml
   tunnel: napa-pds
   credentials-file: /etc/cloudflared/<tunnel-id>.json
   ingress:
     - hostname: napa-node-01.watershed-agent.dev
       service: http://localhost:3000
     - service: http_status:404
   ```
   ```bash
   sudo mkdir -p /etc/cloudflared
   sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
   sudo cp ~/.cloudflared/<tunnel-id>.json /etc/cloudflared/<tunnel-id>.json
   ```

5. **Run the tunnel as a service** (survives reboots):
   ```bash
   sudo cloudflared service install
   sudo systemctl start cloudflared
   sudo systemctl status cloudflared   # confirm it's connected
   ```

6. **Generate PDS secrets**:
   ```bash
   openssl rand --hex 16   # PDS_JWT_SECRET
   openssl rand --hex 16   # PDS_ADMIN_PASSWORD
   openssl ecparam --name secp256k1 --genkey --noout --outform DER \
     | tail --bytes=+8 | head --bytes=32 | od -An -tx1 | tr -d ' \n'
     # ^ PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX (needs a real secp256k1
     #   key, not arbitrary random bytes — this derives one via openssl).
     #   Use `xxd --plain --cols 32` in place of `od -An -tx1 | tr -d ' \n'`
     #   if xxd is installed (sudo apt install xxd if not).
   ```

7. **Copy and fill in env**:
   ```bash
   cp pds.env.example pds.env
   # edit pds.env with the generated secrets — PDS_HOSTNAME and
   # PDS_SERVICE_HANDLE_DOMAINS are already set. PDS_HOSTNAME alone does NOT
   # make the domain valid for account handles — PDS_SERVICE_HANDLE_DOMAINS
   # is what allows it (leading dot = suffix match).
   ```

8. **Create the data directory** referenced in `docker-compose.yml`
   (`/home/cprice/pds-data` by default — adjust to match your Pi's layout):
   ```bash
   mkdir -p /home/cprice/pds-data
   ```

9. **Start the PDS**:
   ```bash
   docker compose up -d
   docker compose logs -f pds        # confirm it comes up clean
   ```

10. **Verify end-to-end** (through the tunnel, not just localhost):
    ```bash
    curl -s https://napa-node-01.watershed-agent.dev/xrpc/_health
    ```

11. **Create the node's account** (mints its did:plc). Recent PDS images
    don't ship `pdsadmin`/`dist/scripts` inside the container — account
    creation is just a regular XRPC call, and works without an invite code
    since `PDS_INVITE_REQUIRED=false`:
    ```bash
    curl -s -X POST https://napa-node-01.watershed-agent.dev/xrpc/com.atproto.server.createAccount \
      -H "Content-Type: application/json" \
      -d '{
        "email": "you@example.com",
        "handle": "napa-node-01.watershed-agent.dev",
        "password": "<account-password>"
      }'
    ```
    Save the returned `did` — that's what goes into `node_config.json` and
    into Synthesis's trusted-publishers list.

## Persistence

Everything durable — accounts, repo records, blob store, PLC rotation key —
lives under `/pds` inside the container, bind-mounted to the host path in
`docker-compose.yml`. Recreating the container without that mount destroys
the node's entire identity and history. Back up that directory like you
would any other credential store, not like a cache.

The tunnel's credentials file (`~/.cloudflared/<tunnel-id>.json`) is also
worth backing up — losing it means recreating the tunnel and re-routing DNS,
though it doesn't affect the PDS's own identity.

## After this is running

- `ATProto/publisher.py`: set `ATPROTO_PDS_URL=https://napa-node-01.watershed-agent.dev`
  (env var, or `pds_url` in `node_config.json`) instead of the `bsky.social`
  default, and use the node account's handle/password instead of the
  `napanode1.bsky.social` app password.
- `Synthesis/subscriber.py`: set the same `ATPROTO_PDS_URL` so it fetches
  from this PDS, and update `Synthesis/publishers.json` with the new did:plc
  in place of the old `bsky.social`-issued one.
