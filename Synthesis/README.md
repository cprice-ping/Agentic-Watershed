# ATProto Synthesis Subscriber

Firehose subscriber and distributed synthesis agent.
Replaces the direct SQLite reads in the original synthesis/agent.py.

## Files

- `subscriber.py` — long-running firehose daemon
- `agent/agent_atproto.py` — synthesis agent reading from subscriber.db
- `agent/agent.py` — original synthesis agent (SQLite-direct, kept for comparison)
- `watershed-subscriber.service` — systemd unit for the subscriber

## Distributed architecture intent

The Synthesis agent is designed to run on a **separate machine** from the node agents.
ATProto is the message bus — not just a publishing endpoint.

```
Bluesky Firehose (wss://bsky.network)
         ↓
  subscriber.py (daemon)
  - verifies DID ∈ trusted registry
  - filters by LEXICON NSID
  - decodes CAR blocks
  - stores to subscriber.db
         ↓
  agent_atproto.py (cron, 6h/18h)
  - reads subscriber.db
  - reasons across domains with Sonnet
  - writes to synthesis.db
         ↓
  publisher.py (cron, 15min after agent)
  - reads synthesis.db
  - publishes to Bluesky as napasynth01 DID (future)
```

Domain agents on the Pi publish observations to ATProto as structured records using
a custom lexicon (`net.cpricedomain.temp.monitor.observation`) and a per-node verified DID.
Synthesis subscribes to the firehose, filters by lexicon, and verifies author DIDs
against `TRUSTED_PUBLISHERS` before acting on any record.

## Setup

### On the Pi (production)

```bash
# subscriber.py lives alongside publisher.py in ATProto/
cp subscriber.py ~/Agentic/ATProto/

cd ~/Agentic/ATProto
source .venv/bin/activate
pip install atproto   # adds firehose support to existing venv
```

### On a laptop (from the repo)

The subscriber and agent both run from the `Synthesis/` directory in-tree.
`subscriber.db` ends up at `Synthesis/data/subscriber.db`; pass that path to the agent.

```bash
cd Synthesis

python3 -m venv .venv
source .venv/bin/activate
pip install anthropic atproto

export ANTHROPIC_API_KEY=sk-ant-...

# 1. Fetch records from trusted publishers (exits when done)
python subscriber.py --db data/subscriber.db

# 2. Run the synthesis agent against the local DB
python agent/agent_atproto.py \
  --subscriber-db data/subscriber.db \
  --dry-run --verbose
```

`subscriber.py` fetches records published in the last 24h by default.
Use `--lookback 48` to go further back. The `--subscriber-db` flag on the
agent accepts any path, so you can also point it at a copy of the Pi's
`subscriber.db` scp'd over for offline testing.

## subscriber.py — fetch mode vs firehose mode

**Default (fetch mode)** — cron-friendly, JIT, no persistent connection.
Calls `com.atproto.repo.listRecords` for each trusted publisher, stores new
records, exits. Consistent with how the node agents work.

**`--firehose` mode** — live stream. Must be running *at the moment* nodes publish
or records are missed. Architecturally inconsistent with the cron-triggered nodes;
kept for testing and low-latency future use.

```
# Fetch mode (default) — run just before synthesis agent
python subscriber.py                   # last 24h of records
python subscriber.py --lookback 48     # last 48h

# Firehose mode
python subscriber.py --firehose        # run until Ctrl-C
python subscriber.py --firehose --once --timeout 120
```

## Cron schedule (fetch mode)

Subscriber runs 10 minutes before synthesis agent to ensure records are present:

```cron
# Fetch records from trusted publishers
50 5,17 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python subscriber.py >> logs/subscriber.log 2>&1

# Synthesis agent (10 min later)
0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent_atproto.py >> logs/agent_atproto.log 2>&1
```

## Run the ATProto synthesis agent

```bash
cd ~/Agentic/Synthesis
source .venv/bin/activate
python agent/agent_atproto.py --dry-run --verbose
```

## Trusted publisher registry

In `subscriber.py`, the `TRUSTED_PUBLISHERS` dict maps DID → node name:

```python
TRUSTED_PUBLISHERS = {
    "did:plc:demqbviei2gxjjq2eqnm2rpi": "napa-node-01",
    # "did:plc:...": "napa-node-02",
}
```

Adding a new node: add its DID here. The subscriber starts accepting its records
immediately. No other configuration needed. Removing a DID: the subscriber stops
accepting new records but existing ones remain in subscriber.db.

## Transition from direct SQLite synthesis

The original `agent/agent.py` reads domain SQLite files directly (filesystem coupling).
The new `agent/agent_atproto.py` reads from `subscriber.db` (ATProto decoupled).

Run both in parallel during transition:
- Keep original cron line for `agent/agent.py`
- Add new cron line for `agent/agent_atproto.py`
- Compare outputs; switch fully when confident

```cron
# Original (filesystem coupled)
0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent.py >> logs/agent.log 2>&1

# ATProto version (firehose decoupled)
0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent/agent_atproto.py >> logs/agent_atproto.log 2>&1
```

## What this enables

Once the subscriber is running and agent_atproto.py is working:

1. **Synthesis can move off the Pi** — copy subscriber.db anywhere, or point
   a remote subscriber at the same firehose. The synthesis agent follows the data.

2. **Multiple nodes, zero config** — a second node (napa-node-02) just needs its
   DID added to TRUSTED_PUBLISHERS. The subscriber picks up its records automatically.

3. **Trust is explicit** — the registry is the trust boundary. Unknown nodes
   publishing valid lexicon records are silently ignored.

4. **Provenance is auditable** — every observation in subscriber.db has a
   publisher_did. The synthesis reasoning includes which nodes contributed.
