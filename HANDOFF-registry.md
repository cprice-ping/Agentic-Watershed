# Handoff — Agent Identity Registry (new repo)

Build an Agent Identity Registry: a lightweight service that mints and manages
`did:web` identities for autonomous agents without human signup flows.

Read `CONTEXT.md` in the Agentic-Watershed repo (github.com/cprice-ping/Agentic-Watershed)
for the full architectural background before starting. The birthright identity,
charter model, and IDP critique sections are directly relevant.

---

## Follow established standards — don't invent where specs exist

Before implementing anything, check these. They cover most of the problem space:

**DID infrastructure:**
- **W3C DID Core** (https://www.w3.org/TR/did-core/) — the DID document format
  is fully specified. Use it exactly. No custom formats.
- **did:web spec** (https://w3c-ccg.github.io/did-method-web/) — specifies exactly
  how `did:web` identifiers map to URLs and how documents are served. Follow it.
- **DIF Universal Resolver** (https://dev.uniresolver.io/) — resolves multiple DID
  methods. Consider whether the registry should integrate with it rather than
  implementing resolution from scratch.
- **walt.id** (https://walt.id) — open source DID/VC stack. Evaluate before building
  the registry from scratch — it may already do most of what's needed.

**Charter = Verifiable Credential:**
- **W3C Verifiable Credentials Data Model** (https://www.w3.org/TR/vc-data-model-2.0/)
  — the charter is a VC issued by the registry (as issuer) to the agent (as subject).
  Using the VC data model gives interoperability for free and makes the charter
  presentable to any VC-aware verifier, including a Ping-protected MCP AuthZ server.
- Charter fields map naturally to VC credential subject claims.

**Agent delegation (person → agent):**
- **IETF draft-ietf-oauth-identity-chaining** — the "agent acting on behalf of person"
  delegation flow. Active draft, directly relevant to the MCP AuthZ integration.
- **GNAP (RFC 9635)** — designed for delegated authorization without the
  human-in-the-loop assumption of OAuth 2.0. More agent-native than OAuth.
- **OID4VC / OID4VP** (https://openid.net/sg/openid4vc/) — OpenID Foundation's VC
  issuance and presentation specs. Relevant when an agent presents its charter VC
  to an MCP AuthZ server to obtain a scoped token.
- **RFC 8693 Token Exchange** — sub=agent DID, act=human principal. The token shape
  when a Ping-protected resource server needs to see both identities.

**MCP AuthZ:**
- Check the current MCP specification for any emerging agent identity/AuthZ guidance
  before designing the charter schema — align with what MCP expects.

---

## Core principle

Agents generate their own keypairs locally. The registry never sees private keys.
It stores public keys, mints DIDs, serves DID documents, and issues charter VCs.

The registry itself has a `did:web` at the domain root. Agent DID documents
reference it as controller/issuer — making the registry the trust anchor
without requiring a central CA.

---

## API

```
POST /agents               → register pubkey + charter → returns DID + signed charter VC
GET  /agents/{id}/did.json → DID document (did:web resolution endpoint — per did:web spec)
GET  /agents/{id}/charter  → agent charter as W3C VC
POST /agents/{id}/rotate   → key rotation, update DID document
DELETE /agents/{id}        → revocation — DID document returns tombstone
```

---

## Charter schema

Use the **W3C VC data model** — the charter is a Verifiable Credential:

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://cpricedomain.net/contexts/agent-charter/v1"
  ],
  "type": ["VerifiableCredential", "AgentCharterCredential"],
  "issuer": "did:web:cpricedomain.net",
  "credentialSubject": {
    "id": "did:web:cpricedomain.net:agents:napanode01",
    "name": "napa-node-01",
    "capabilities": ["observe", "publish"],
    "scope": "Napa Valley environmental monitoring — watershed, weather, AQI",
    "intent": "Collect domain sensor data, reason locally, publish observations",
    "operator": "did:web:cpricedomain.net"
  }
}
```

The registry signs this VC with its own private key. Any verifier can check the
signature against the registry's DID document without calling back to the registry.

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
- `PyLD` or `jsonld` for JSON-LD / VC processing if needed

---

## Client library

Deliver a `registry_client.py` (or installable package) that consuming projects import:

```python
# Called once at agent setup — generates keypair, registers with registry,
# returns DID + stores charter VC locally
did = registry.provision(charter: dict) -> str

# Resolves a DID, returns charter VC. Cached with TTL.
# Verifies the VC signature against the registry's DID — no trust on wire.
charter = registry.verify(did: str) -> dict | None

# Signs a payload with the agent's local private key
signed = registry.sign(record: dict, did: str) -> dict
```

Private keys stored locally by the client (e.g. `~/.agent/keys/{did}.pem`).
Charter VCs stored alongside (`~/.agent/charters/{did}.json`).

---

## What this is NOT

- Not an IDP. Does not issue tokens, manage sessions, or handle human login.
- Not a PDS. Does not store ATProto records or serve the firehose.
- Not a key custodian. Private keys never leave the agent's local environment.

---

## First consumer

Agentic-Watershed (github.com/cprice-ping/Agentic-Watershed).
See `HANDOFF-watershed-registry.md` in that repo for the integration spec.
