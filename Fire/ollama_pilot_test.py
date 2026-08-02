"""
Throwaway pilot script — NOT wired into agent.py.
Tests whether a smaller/cheaper model can do the Fire agent's actual job:
read real recent hotspot/observation data, produce a structured verdict,
via a real schema-constrained call (not just "please respond in JSON").

Three backends, same real data, same real schema — for a direct comparison:
  --backend ollama      Local model via Ollama (default)
  --backend openrouter  Hosted model via OpenRouter (needs OPENROUTER_API_KEY) —
                        routes to whichever upstream provider it picks per
                        request (seen: Together, SiliconFlow, Parasail),
                        which is exactly why reasoning-disable reliability
                        varied call to call.
  --backend together    Hosted model via Together.ai directly (needs
                        TOGETHER_API_KEY) — no routing variability, and uses
                        Qwen3.5's actual native reasoning toggle
                        (chat_template_kwargs.enable_thinking) instead of
                        OpenRouter's translated `reasoning.effort` parameter.

Usage:
  python3 ollama_pilot_test.py                                   # local, qwen2.5:3b
  python3 ollama_pilot_test.py --model qwen3.5:4b                # local, different model
  python3 ollama_pilot_test.py --backend openrouter               # OpenRouter, qwen3.5-9b
  python3 ollama_pilot_test.py --backend openrouter --model qwen/qwen3.5-9b
  python3 ollama_pilot_test.py --backend together                 # Together.ai direct
"""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent / "data" / "fire.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TOGETHER_URL = "https://api.together.xyz/v1/chat/completions"
# LM Studio's OpenAI-compatible local server. Override with LMSTUDIO_HOST env
# var when running this on a different machine than the one serving the
# model (e.g. this on the Pi, LM Studio on a Mac on the same LAN) —
# LMSTUDIO_HOST=192.168.1.50:1234
LMSTUDIO_URL = f"http://{os.environ.get('LMSTUDIO_HOST', 'localhost:1234')}/v1/chat/completions"
DEFAULT_MODEL = {
    "ollama": "qwen2.5:3b-instruct-q4_K_M",
    "openrouter": "qwen/qwen3.5-9b",
    "together": "Qwen/Qwen3.5-9B",
    "lmstudio": "google/gemma-4-31b-it",
}

SYSTEM_PROMPT = """You are a fire-detection monitoring agent for Napa Valley. You are NOT
assessing fire weather or smoke — only satellite-detected heat sources (hotspots).
Flag if any hotspot is within 20 miles, or any high-confidence hotspot is within 50 miles,
or FRP is rising across recent detections."""

# Same shape as Fire/agent.py's _ASSESSMENT_TOOL input_schema
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "flagged": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["summary", "flagged", "reasoning"],
}


def gather_real_context() -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    hotspots = conn.execute(
        """
        SELECT latitude, longitude, acq_date, acq_time, satellite,
               confidence, frp, distance_mi
        FROM hotspots
        ORDER BY distance_mi ASC
        LIMIT 10
        """
    ).fetchall()

    obs = conn.execute(
        """
        SELECT observed_at, summary, flagged
        FROM agent_observations
        ORDER BY observed_at DESC
        LIMIT 3
        """
    ).fetchall()

    poll = conn.execute(
        "SELECT polled_at, status, hotspots_fetched, hotspots_new FROM polls ORDER BY polled_at DESC LIMIT 1"
    ).fetchone()

    conn.close()

    sections = [
        "=== LAST POLL STATUS ===\n" + json.dumps(dict(poll) if poll else {}, indent=2),
        "=== NEAREST HOTSPOTS ===\n" + json.dumps([dict(r) for r in hotspots], indent=2),
        "=== PREVIOUS OBSERVATIONS (memory) ===\n" + json.dumps([dict(r) for r in obs], indent=2),
    ]
    return "\n\n".join(sections)


def call_ollama(model: str, context: str, think: bool) -> tuple[dict, float]:
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": context,
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "think": think,
    }
    start = time.monotonic()
    # Generous timeout — the point right now is finding out the real number,
    # not enforcing a limit. Tighten once there's an actual baseline.
    resp = httpx.post(OLLAMA_URL, json=payload, timeout=600)
    resp.raise_for_status()
    elapsed = time.monotonic() - start
    return resp.json(), elapsed


def call_openrouter(model: str, context: str) -> tuple[dict, float]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set — get one at https://openrouter.ai/keys")

    # Standard OpenAI-style structured-output request. Whether this is
    # actually *enforced* depends on which upstream provider OpenRouter
    # routes this model to — that reliability is exactly what this script
    # is here to check, not something to assume works like Ollama's local
    # `format` constraint does.
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "assessment", "strict": True, "schema": RESPONSE_SCHEMA},
        },
        # Confirmed in practice: qwen/qwen3.5-9b via OpenRouter always emits
        # a long `reasoning` trace before `content`, and 4096 total tokens
        # wasn't enough for the reasoning pass alone — content came back
        # null with finish_reason="length" on every run, not just some.
        # effort="none" asks OpenRouter to skip the reasoning pass entirely
        # (not just hide it — "exclude": true would still generate it and
        # burn the budget, which doesn't fix this). Some models reject a
        # request to disable mandatory reasoning; if that happens here,
        # max_tokens is still raised well above 4096 as a fallback so a
        # shortened reasoning pass has room to leave content non-empty.
        "reasoning": {"effort": "none"},
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    start = time.monotonic()
    resp = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    if resp.status_code >= 400:
        # A model that requires mandatory reasoning may reject this
        # outright (seen in the wild for some OpenRouter-routed models) —
        # surface the actual error body instead of a bare traceback.
        print(f"!!! OpenRouter returned {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()
    elapsed = time.monotonic() - start
    return resp.json(), elapsed


def call_together(model: str, context: str) -> tuple[dict, float]:
    api_key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TOGETHER_API_KEY is not set — get one at https://api.together.ai/settings/api-keys")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "assessment", "strict": True, "schema": RESPONSE_SCHEMA},
        },
        # Qwen3.5's own native thinking-mode toggle, set via the chat
        # template rather than a cross-provider translated parameter —
        # this is what OpenRouter's `reasoning.effort` gets (inconsistently)
        # translated into depending on which upstream it routes to. Calling
        # Together directly means this is the real mechanism, not a guess
        # at how faithfully OpenRouter forwarded it.
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    start = time.monotonic()
    max_retries = 5
    for attempt in range(max_retries):
        resp = httpx.post(TOGETHER_URL, json=payload, headers=headers, timeout=300)
        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", 2 ** attempt * 2))
            print(f"!!! Together 429 (attempt {attempt + 1}/{max_retries}) — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        break
    if resp.status_code >= 400:
        print(f"!!! Together returned {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()
    elapsed = time.monotonic() - start
    return resp.json(), elapsed


def call_lmstudio(model: str, context: str) -> tuple[dict, float]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "assessment", "strict": True, "schema": RESPONSE_SCHEMA},
        },
        "max_tokens": 8192,
    }

    start = time.monotonic()
    resp = httpx.post(LMSTUDIO_URL, json=payload, timeout=900)
    if resp.status_code >= 400:
        print(f"!!! LM Studio returned {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()
    elapsed = time.monotonic() - start
    return resp.json(), elapsed


def extract_response_text(backend: str, result: dict) -> str | None:
    """Ollama's raw response shape differs from the others — normalise to
    the raw text the model produced, or None if it's not where expected."""
    if backend == "ollama":
        return result.get("response")
    # OpenRouter and Together both use the same OpenAI-compatible
    # chat-completions shape
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def check_finish_reason(backend: str, result: dict) -> None:
    """Surface truncation explicitly instead of leaving 'empty response' to
    be diagnosed by eye from the raw dump — this is exactly what caused the
    long-reasoning-eats-the-budget failure seen in practice."""
    if backend == "ollama":
        done_reason = result.get("done_reason")
        if done_reason and done_reason != "stop":
            print(f"!!! done_reason='{done_reason}' — response likely truncated, not a clean finish")
        return
    try:
        finish_reason = result["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return
    if finish_reason and finish_reason != "stop":
        print(f"!!! finish_reason='{finish_reason}' — response likely truncated (e.g. hit max_tokens), "
              f"not a clean finish. If this is 'length', raise max_tokens further.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["ollama", "openrouter", "together", "lmstudio"], default="ollama")
    parser.add_argument("--model", default=None,
                        help="Model tag/id to test (default depends on --backend)")
    parser.add_argument("--think", action="store_true",
                        help="[ollama only] Allow reasoning-capable models to emit a thinking "
                             "trace (off by default — a short structured verdict doesn't need "
                             "one, and it can eat the generation budget before the real answer)")
    args = parser.parse_args()
    model = args.model or DEFAULT_MODEL[args.backend]

    context = gather_real_context()
    print(f"--- Backend: {args.backend}  |  Model: {model}  |  Context size: {len(context)} chars ---\n")

    if args.backend == "ollama":
        result, elapsed = call_ollama(model, context, args.think)
    elif args.backend == "openrouter":
        result, elapsed = call_openrouter(model, context)
    elif args.backend == "lmstudio":
        result, elapsed = call_lmstudio(model, context)
    else:
        result, elapsed = call_together(model, context)

    print(f"--- Raw response object ({elapsed:.1f}s) ---")
    print(json.dumps(result, indent=2)[:3000])

    check_finish_reason(args.backend, result)

    response_text = extract_response_text(args.backend, result)
    print(f"\n--- response text ---")
    print(response_text if response_text is not None else "<not found in expected location>")

    try:
        parsed = json.loads(response_text)
        print("\n--- Parsed OK ---")
        print(f"flagged: {parsed.get('flagged')}")
        print(f"summary: {parsed.get('summary')}")
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"\n--- FAILED TO PARSE: {exc} ---")


if __name__ == "__main__":
    main()
