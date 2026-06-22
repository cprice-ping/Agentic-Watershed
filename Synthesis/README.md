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
a custom lexicon (`com.napavalley.monitor.observation`) and a per-node verified DID.
Synthesis subscribes to the firehose, filters by lexicon, and verifies author DIDs
against `TRUSTED_PUBLISHERS` before acting on any record.

## Setup

```bash
# subscriber.py lives alongside publisher.py in ATProto/
cp subscriber.py ~/Agentic/ATProto/
cp agent/agent_atproto.py ~/Agentic/Synthesis/agent/

cd ~/Agentic/ATProto
source .venv/bin/activate
pip install atproto   # adds firehose support to existing venv
```

## Test the subscriber

```bash
cd ~/Agentic/ATProto
source .venv/bin/activate
python subscriber.py --once --timeout 120
```

This connects to the firehose for 2 minutes and prints any matching records.
Since your node publishes every 6 hours, you may need to wait or trigger
a manual publisher run first.

Check what's been received:
```bash
sqlite3 data/subscriber.db \
  "SELECT received_at, node_id, observation_type, flagged, substr(summary,1,80) FROM observations ORDER BY received_at DESC LIMIT 10;"
```

## Run subscriber as a daemon (systemd)

```bash
sudo cp watershed-subscriber.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable watershed-subscriber
sudo systemctl start watershed-subscriber
sudo systemctl status watershed-subscriber
```

Check logs:
```bash
tail -f ~/Agentic/ATProto/logs/subscriber.log
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
