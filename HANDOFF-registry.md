# Handoff — Agent Identity Registry (new repo)

Build an Agent Identity Registry: a lightweight service that mints and manages
`did:web` identities for autonomous agents without human signup flows.

Read `CONTEXT.md` in the Agentic-Watershed repo (github.com/cprice-ping/Agentic-Watershed)
for the full architectural background before starting. The birthright identity,
charter model, and IDP critique sections are directly relevant.

---

## Core principle

Agents generate their own keypairs locally. The registry never sees private keys.
It stores public keys, mints DIDs, serves DID documents, and records agent charters.

The registry itself has a `did:web` at the domain root. Agent DID documents
reference it as controller/issuer — making the registry the trust anchor
without requiring a central CA.

---

## API

```
POST /agents               → register pubkey + charter → returns DID
GET  /agents/{id}/did.json → DID document (did:web resolution endpoint)
GET  /agents/{id}/charter  → agent charter (capabilities, scope, intent)
POST /agents/{id}/rotate   → key rotation, update DID document
DELETE /agents/{id}        → revocation — DID document returns tombstone
```

---

## Charter schema

Needs designing. Fields to include:
- `name` — human-readable agent name
- `capabilities` — list of declared capabilities (e.g. `["observe", "synthesise", "publish"]`)
- `scope` — what domains/resources the agent operates over
- `intent` — plain language description of what the agent does
- `operator` — DID or identifier of the operating entity
- `createdAt` — timestamp

Probably JSON-LD or a simple JSON schema. Should be extensible.

---

## DID document format

Standard W3C DID document, served at the `did:web` resolution URL:

```json
{
  "@context": ["https://www.w3.org/ns/did/v1"],
  "id": "did:web:cpricedomain.net:agents:napanode01",
  "controller": "did:web:cpricedomain.net",
  "verificationMethod": [{
    "id": "did:web:cpricedomain.net:agents:napanode01#key-1",
    "type": "JsonWebKey2020",
    "controller": "did:web:cpricedomain.net:agents:napanode01",
    "publicKeyJwk": { ... }
  }],
  "authentication": ["did:web:cpricedomain.net:agents:napanode01#key-1"]
}
```

The registry's own DID document lives at `https://cpricedomain.net/.well-known/did.json`.

---

## Stack

- Python, FastAPI, SQLite
- Standard JWK keypairs (`cryptography` library)
- No external dependencies beyond these

---

## Client library

Deliver a `registry_client.py` (or installable package) that consuming projects import:

```python
# Called once at agent setup — generates keypair, registers with registry
did = registry.provision(charter: dict) -> str

# Resolves a DID, returns charter. Cached with TTL.
charter = registry.verify(did: str) -> dict | None

# Signs a record with the agent's local private key
signed = registry.sign(record: dict, did: str) -> dict
```

Private keys stored locally by the client (e.g. `~/.agent/keys/{did}.pem`).
The `verify` call is the one consuming projects use most — it's the trust check
at record ingestion time.

---

## What this is NOT

- Not an IDP. Does not issue tokens, manage sessions, or handle human login.
- Not a PDS. Does not store ATProto records or serve the firehose.
- Not a key custodian. Private keys never leave the agent's local environment.

---

## First consumer

Agentic-Watershed (github.com/cprice-ping/Agentic-Watershed).
See `HANDOFF-watershed-registry.md` in that repo for the integration spec.
