# Ephemeral investigator agents — starting point for a new session

## Context

**The question that started this:** how do agents spawn agents and divide tasks, and
is that mechanism worth borrowing?

**What we concluded about the mechanism.** Harness-level subagent spawning (the
Claude Code `Agent` tool and equivalents) is entirely a harness function. The model
emits a tool call; the harness starts a *second inference loop* with a fresh message
array, its own system prompt from an agent definition, and a single user message —
the prompt string the parent wrote. The child's transcript never enters the parent's;
the parent gets only the child's final message as a `tool_result`. Context isolation
is the whole value proposition, and the cost is that **the child starts cold, peers
can't see each other's work, and the child has no identity of its own** — it inherits
the parent's credentials and permission mode wholesale.

That last part is the interesting failure. Agentic-Watershed already solves it, from
the other direction:

| | Harness subagents | Watershed today |
|---|---|---|
| Coordination | prompt string in, summary string out | published records on a shared bus |
| Peers see each other's work | never | yes, by design |
| Agent identity | none — inherits parent's | DID per node, verified at the boundary |
| Work products | ephemeral, die with the child | durable, addressable, attributable |
| Topology | dynamic, hierarchical | **static, hardcoded** |

Neither has both. That gap is the thing worth building.

## What we are NOT doing

Two shapes were considered and rejected — record them so they don't get re-proposed:

- **Synthesis spawning the domain agents.** Rejected. Cron is the correct trigger for
  perception: the domain agents sample a world that changes on USGS/FIRMS/NWS cadence,
  not on a consumer's request. Spawning them would couple observation frequency to
  synthesis frequency and destroy the continuous time series that prediction-tracking
  depends on. It would also make them Synthesis's children — which naturally means
  inheriting its credentials, reproducing the exact flattening the DID model exists to
  prevent — and turn the PDS into RPC with extra steps.
- **Spawning as a replacement for cron.** Rejected. Additive only. Cron stays as-is.

## The thing we ARE doing

> **Domain agents are perception on a schedule. A spawned agent is investigation on demand.**

The current design works because the *questions* are fixed and known in advance — four
domains, four collectors. Spawning earns its place only where Synthesis reaches a
conclusion implying a question nobody wrote a collector for.

Worked example: `River` flow dropping fast + `Weather` low humidity + a `Fire`
detection 40km upwind raises *"what is fuel moisture and wind forecast along the
corridor between that detection and the watershed?"* — owned by no domain agent,
because nobody anticipated the combination. You cannot cron an agent for every
cross-domain question; there are combinatorially too many and almost none are ever
worth asking.

**Critically, the investigator is a new peer, not a child process:**

- gets its own DID from the Registry
- gets a charter scoped to the question (weather + fire reads, this corridor, hours)
- publishes its finding to the same PDS as a first-class lexicon record
- expires

Synthesis reads the result back through the same verified path as every other record.
No parent/child channel, no inherited credentials, no privileged link. Synthesis
*caused it to exist* but has no special access to it. **The spawn is an identity
event, not a process event.**

### Honest framing of the motivation

Napa does not need this. The set of genuinely interesting cross-domain questions is
small enough to enumerate — write 5–10 fixed agents and you're done. What justifies
the build is the repo's own stated thesis ("the concrete surface is Napa, the actual
subject is the architecture"). Ephemeral, charter-scoped, self-identifying agents have
no good reference implementation anywhere as of now, and ~80% of the substrate already
exists here. That is a real reason — but it means the build should stay aimed at
**demonstrating the identity mechanics**, not at better fire advisories. If a decision
is ever ambiguous, resolve it toward showing the mechanism.

---

## Key finding: the attenuation primitive already exists

`Agentic-DID-Registry` is further along than `HANDOFF-registry.md` implies. It already
implements the exact clamp this pattern needs:

> *"The voucher bounds, the agent asserts, and the registry clamps. Capabilities are
> not copied from the voucher into the charter. The agent declares its capabilities in
> the charter it submits, and the registry issues `charter ≤ voucher`."* — Registry `README.md`

Implemented in `app/voucher.py` (`VoucherGrant.capabilities`, `verify_voucher`,
`load_operator_keys`); over-claim is rejected with `403`. `registry_client.py` exposes
`provision(charter, voucher)`, `verify(did)`, `sign(record, did)`, `present*()`,
`rotate(did)`. Revocation infrastructure exists in `app/status_list.py`.

**So the entire delta between what exists and agent-spawns-agent is: who is allowed to
sign a voucher.** Today, a human operator's offline key. The pattern needs Synthesis to
hold a voucher-signing key whose ceiling is its own charter, producing a two-step clamp:

```
investigator_charter  ≤  voucher_from_synthesis  ≤  synthesis_charter
        ↑ clamp already implemented          ↑ clamp to be added
```

That is a much smaller build than it first appeared.

---

## Which repo to start with: **Agentic-Watershed**

Not the Registry, despite the pattern being an identity pattern. The reason is a hard
prerequisite:

**Attenuation needs a parent charter to attenuate from, and Synthesis does not have
one.** Per `CONTEXT.md:455`, Synthesis is still `did:plc:clcw2dxrd6qma45gy3oozjwa` /
`napasynth01.bsky.social` — not registry-provisioned. `provision_watershed.py` covers
the `napa-node-01` domain agents only; Synthesis runs on Azure Container Apps and was
never wired in. Until Synthesis holds a registry-issued charter there is literally
nothing to bound an investigator by, and step 2 cannot be specified.

This step is also **independently valuable**: it closes the hardcoded-`TRUSTED_PUBLISHERS`
trust gap already flagged at `CONTEXT.md:352` and `CONTEXT.md:437`, whether or not
spawning is ever built. Low risk of wasted work.

### Sequence

**Step 1 — Watershed: provision Synthesis against the Registry.** Mostly specced
already in `HANDOFF-watershed-registry.md`; extend `provision_watershed.py`'s `AGENTS`
model to cover the Azure-hosted Synthesis agent. Replace the static `TRUSTED_PUBLISHERS`
lookup in `subscriber.py` with `RegistryClient.verify(did)` + charter cache (TTL). ATProto
record structure, lexicon, cron schedule, and Bluesky publishing all stay unchanged —
identity layer only. Ends with: Synthesis holds a registry `did:web` + charter, and
verifies node records through the registry rather than a hardcoded dict.

**Step 2 — Registry: agent-issued vouchers.** Allow a voucher signed by a
charter-holding agent rather than only an operator key (`app/voucher.py`,
`load_operator_keys` is the seam). Registry must clamp the issued voucher against the
*issuer's own* charter before the existing `charter ≤ voucher` clamp runs. Respect the
repo's stated boundary — `docs/scope-and-boundaries.md`, and the README's warning that
"the pull to make it do more is structural." The registry still only **issues**; the
decision to spawn stays in Watershed. Also needed: sub-day charter lifetime —
`CHARTER_TTL_DAYS` (`app/config.py:37`) and `vc.py`'s `ttl_days` are day-granular;
an investigator wants hours. Decide between a duration-typed TTL and leaning on
`status_list.py` for immediate revocation.

**Step 3 — Watershed: lexicon + the investigator itself.** New record types for an
investigation *request* and an investigation *finding*, alongside
`net.cpricedomain.temp.monitor.observation`. Synthesis gains the ability to conclude
"this needs investigating," mint a scoped voucher, and let the investigator self-enroll,
run, publish, expire. Synthesis consumes the finding through the normal verified read
path.

---

## Open questions to resolve in the new session

1. **Where does the investigator run?** Pi node, a second Azure job, or ephemeral
   container. Affects how the private key is generated and held — `registry_client.py`
   assumes `~/.agent/keys/{did}.pem`, which is awkward for something meant to vanish.
2. **Does an expired investigator's DID stay resolvable?** Its published finding is
   durable and stays on the bus; verifying that record later requires the DID to still
   resolve. Expiry of the *charter* and expiry of the *identifier* likely need to differ.
3. **Trust bootstrap.** `CONTEXT.md:437`'s open question in its sharpest form: Synthesis
   vouches for a DID no human ever approved. Is the registry's signature over a
   Synthesis-issued voucher sufficient for `subscriber.py` to trust the finding, or does
   a finding from an ephemeral peer need a distinct trust tier?
4. **Runaway spawning.** Nothing yet bounds how many investigators Synthesis may create.
   Rate/quota belongs in the charter as a claim, but the registry makes no authorization
   decisions — so who enforces it?

## Verification

- Step 1: run `provision_watershed.py` against the live registry for Synthesis; confirm
  a `did:web` + charter VC are issued and stored. Run a real synthesis cycle and confirm
  it fetches, verifies publisher DIDs *via the registry*, and still posts to
  `napasynth01.bsky.social`. Regression check: node records must still verify.
- Step 2: registry tests under `tests/` — an agent-issued voucher exceeding the issuer's
  charter must be rejected with `403`, mirroring the existing over-claim test.
- Step 3: end-to-end — force the trigger condition, confirm an investigator DID is
  minted with strictly narrower capabilities than Synthesis's, that its finding appears
  on the PDS as a verifiable record, and that its charter is unusable after expiry.

## Status

**Nothing here is built.** This is a design starting point, not a record of work done.
No code in this repo or in `Agentic-DID-Registry` has been changed for it.

Read alongside:

- `CONTEXT.md` — architecture decisions; the open trust question at `:437` and the
  interim `TRUSTED_PUBLISHERS` boundary at `:352` are what step 1 closes
- `HANDOFF-watershed-registry.md` — the registry integration step 1 depends on and
  largely reuses
- `HANDOFF-registry.md` — the registry's own brief, in `Agentic-DID-Registry`

Resolve open question #2 (charter lifetime vs. identifier lifetime) before starting
step 2 — it constrains the registry's data model rather than merely configuring it.
