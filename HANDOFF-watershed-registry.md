# Handoff — Agentic-Watershed × Agent Identity Registry integration

Integrate the Agentic-Watershed project with the Agent Identity Registry.
This repo already publishes to Bluesky and has a working ATProto pipeline.
This handoff replaces the identity layer underneath without breaking publishing.

Read `CONTEXT.md` in this repo first — particularly the sections on birthright
identity, the DID onboarding problem, and the reconciliation path. Then read
`HANDOFF-registry.md` to understand the registry being integrated against.

---

## What stays the same

- Bluesky publishing accounts (`napanode1.bsky.social`, `napasynth01.bsky.social`)
  remain as the human-readable publishing handles
- ATProto record structure and lexicon unchanged
- Cron schedule unchanged
- `subscriber.py` fetch mode unchanged

Only the **identity layer** changes: how agents prove who they are, and how
the subscriber decides whether to trust an incoming record.

---

## Three changes required

### 1. Provision — agent setup (one-time)

Each agent calls `registry.provision(charter)` once at setup time.
Gets back a DID. Private key stored locally.

Replace the manual "create a Bluesky account" setup step with:

```python
from registry_client import registry

charter = {
    "name": "napa-node-01",
    "capabilities": ["observe", "publish"],
    "scope": "Napa Valley environmental monitoring — watershed, weather, AQI",
    "intent": "Collect domain sensor data, reason locally, publish observations",
    "operator": "did:web:cpricedomain.net",
}
did = registry.provision(charter)
print(f"Provisioned: {did}")
# did:web:cpricedomain.net:agents:napanode01
```

Add a `provision.py` script per stack (Watershed, Weather, AQI, Synthesis)
that runs this once and records the DID in the stack's config.

### 2. Sign — at publish time

`ATProto/publisher.py` currently signs records implicitly via the Bluesky
App Password session. Replace with explicit signing via the registry client:

```python
from registry_client import registry

# Before creating the ATProto record
signed_record = registry.sign(record, did=NODE_DID)
record_uri = session.create_record(LEXICON, signed_record)
```

`NODE_DID` read from environment or local config (set during provisioning).

### 3. Verify — at subscribe time

`Synthesis/subscriber.py` currently checks a static `TRUSTED_PUBLISHERS` dict.
Replace with a live registry lookup that checks both identity and capabilities:

```python
from registry_client import registry

# In the message handler / fetch loop, replace:
if publisher_did not in TRUSTED_PUBLISHERS:
    return

# With:
charter = registry.verify(publisher_did)
if not charter or "observe" not in charter.get("capabilities", []):
    log.warning("Rejected record from %s — not in registry or missing 'observe' capability", publisher_did)
    return

node_id = charter.get("name", publisher_did)
```

The `verify` call is cached with TTL in the client — no registry round-trip
on every record. Trust becomes capability-aware: a known DID whose charter
lacks `observe` is rejected even if its identity is valid.

---

## New environment variables

```bash
# Node identity (set after provisioning)
NODE_DID=did:web:cpricedomain.net:agents:napanode01

# Synthesis identity (set after provisioning)
SYNTH_DID=did:web:cpricedomain.net:agents:napasynth01

# Registry endpoint
AGENT_REGISTRY_URL=https://cpricedomain.net
```

---

## Files to modify

| File | Change |
|------|--------|
| `ATProto/publisher.py` | Add `registry.sign()` before `create_record()` |
| `Synthesis/subscriber.py` | Replace `TRUSTED_PUBLISHERS` dict with `registry.verify()` |
| `Synthesis/publisher.py` | Add `registry.sign()` before `create_record()` |
| Each stack's config/env | Add `NODE_DID` / `SYNTH_DID` after provisioning |

---

## Files to add

| File | Purpose |
|------|---------|
| `registry_client.py` | Comes from the Agent Identity Registry repo |
| `ATProto/provision.py` | One-time node provisioning script |
| `Synthesis/provision.py` | One-time synthesis provisioning script |

---

## Bluesky App Passwords

After migration, `BSKY_APP_PASSWORD` and `BSKY_SYNTH_APP_PASSWORD` are still
needed for Bluesky session auth (posting) — but they are no longer the identity.
The DID and signing key are. The App Password is just an API credential for the
publishing transport.
