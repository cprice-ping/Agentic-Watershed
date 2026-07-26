"""
Throwaway pilot script — NOT wired into agent.py.
Tests whether a smaller/cheaper model can do the Fire agent's actual job:
read real recent hotspot/observation data, produce a structured verdict,
via a real schema-constrained call (not just "please respond in JSON").

Two backends, same real data, same real schema — for a direct comparison:
  --backend ollama      Local model via Ollama (default)
  --backend openrouter  Hosted model via OpenRouter (needs OPENROUTER_API_KEY)

Usage:
  python3 ollama_pilot_test.py                                   # local, qwen2.5:3b
  python3 ollama_pilot_test.py --model qwen3.5:4b                # local, different model
  python3 ollama_pilot_test.py --backend openrouter               # OpenRouter, qwen3.5-9b
  python3 ollama_pilot_test.py --backend openrouter --model qwen/qwen3.5-9b
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
DEFAULT_MODEL = {
    "ollama": "qwen2.5:3b-instruct-q4_K_M",
    "openrouter": "qwen/qwen3.5-9b",
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
        # Some models (Qwen3.5 included) emit a separate, often verbose
        # `reasoning` field before `content`. Without a generous ceiling
        # here, that reasoning can consume the whole budget and leave
        # `content` truncated to an empty string — seen in practice, not
        # hypothetical. 4096 is deliberately generous for a task whose
        # actual answer is a few sentences.
        "max_tokens": 4096,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    start = time.monotonic()
    resp = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    elapsed = time.monotonic() - start
    return resp.json(), elapsed


def extract_response_text(backend: str, result: dict) -> str | None:
    """Both backends' raw response shapes differ — normalise to the raw
    text the model produced, or None if it's not where expected."""
    if backend == "ollama":
        return result.get("response")
    # OpenRouter/OpenAI chat completions shape
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
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="ollama")
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
    else:
        result, elapsed = call_openrouter(model, context)

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
