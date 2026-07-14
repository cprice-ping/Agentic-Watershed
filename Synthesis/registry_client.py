"""
Agent Identity Registry client
-------------------------------
Thin client for the PIP (Policy Information Point) endpoints exposed by
the Agentic-DID-Registry (github.com/cprice-ping/Agentic-DID-Registry,
deployed at registry.cpricedomain.net). The registry issues per-agent
charters (W3C Verifiable Credentials) independent of ATProto/PDS identity —
see CONTEXT.md's "Agent Identity Registry" section for the full design.

This module answers exactly one question: "does this agent DID currently
hold a valid, unrevoked charter with the 'observe' capability?" It does
NOT make authorization decisions beyond that — per the registry's own
stated boundary, PIP endpoints "return validity, claims, and status but
never grant/deny authorization." The decision of what to do with that
information (accept/reject a record) belongs to the caller.

Response field names here are a best-effort match against the registry's
documented behavior ("`/verify` returns `{valid, claims, status, ...}`") —
the README doesn't publish an exact schema for `/resolve`. Adjust
_parse_attributes() once tested against the live registry; nothing else
in this module should need to change.

Env vars:
  AGENT_REGISTRY_URL              default https://registry.cpricedomain.net
  AGENT_REGISTRY_CACHE_TTL_SECONDS default 900 (15 min)
  AGENT_REGISTRY_FAIL_CLOSED      default "false" — if a charter lookup
                                   errors (network, unexpected shape), treat
                                   the agent as untrusted ("true") or as
                                   trusted-by-default ("false"). Default is
                                   fail-open since nothing is enrolled with
                                   the registry yet; flip once confident.
"""

import logging
import os
import time

import httpx

REGISTRY_URL = os.environ.get("AGENT_REGISTRY_URL", "https://registry.cpricedomain.net").rstrip("/")
CACHE_TTL_SECONDS = float(os.environ.get("AGENT_REGISTRY_CACHE_TTL_SECONDS", "900"))
FAIL_CLOSED = os.environ.get("AGENT_REGISTRY_FAIL_CLOSED", "false").strip().lower() == "true"

log = logging.getLogger("synthesis.registry_client")

# did -> (fetched_at monotonic seconds, attributes dict or None)
_cache: dict[str, tuple[float, dict | None]] = {}


def _parse_attributes(payload: dict) -> dict:
    """
    Normalise whatever shape /resolve actually returns into
    {valid: bool, capabilities: list[str], status: str}.
    Best-effort against multiple plausible field names until confirmed
    against the live registry.
    """
    valid = payload.get("valid")
    if valid is None:
        status = str(payload.get("status", "")).lower()
        valid = status not in ("revoked", "expired", "invalid", "")

    claims = payload.get("claims") if isinstance(payload.get("claims"), dict) else {}
    capabilities = payload.get("capabilities") or claims.get("capabilities") or []

    return {
        "valid": bool(valid),
        "capabilities": list(capabilities),
        "status": payload.get("status", "unknown"),
    }


def resolve_agent(did: str) -> dict | None:
    """
    Look up an agent DID's charter attributes via the registry's PIP
    endpoint (GET /resolve?subject={did}). Cached with a TTL to avoid
    hitting the registry on every record. Returns None on any failure
    (network error, non-200, unexpected shape) — callers decide what
    "unknown" means via FAIL_CLOSED.
    """
    now = time.monotonic()
    cached = _cache.get(did)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        resp = httpx.get(f"{REGISTRY_URL}/resolve", params={"subject": did}, timeout=10)
        resp.raise_for_status()
        attrs = _parse_attributes(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Registry lookup failed for %s: %s", did, exc)
        attrs = None

    _cache[did] = (now, attrs)
    return attrs


def has_capability(did: str, capability: str = "observe") -> bool:
    """
    True if the agent DID holds a valid, unrevoked charter declaring the
    given capability. On lookup failure, returns FAIL_CLOSED's inverse
    (i.e. trusted by default unless AGENT_REGISTRY_FAIL_CLOSED=true).
    """
    attrs = resolve_agent(did)
    if attrs is None:
        if FAIL_CLOSED:
            log.warning("Registry unreachable for %s and fail-closed is on — rejecting", did)
            return False
        log.warning("Registry unreachable for %s — fail-open, trusting for now", did)
        return True

    if not attrs["valid"]:
        log.warning("Charter for %s is not valid (status=%s) — rejecting", did, attrs["status"])
        return False

    if capability not in attrs["capabilities"]:
        log.warning("Charter for %s lacks capability '%s' (has %s) — rejecting",
                    did, capability, attrs["capabilities"])
        return False

    return True
