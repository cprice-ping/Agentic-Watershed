"""
Throwaway pilot script — NOT wired into agent.py.
Same purpose as Fire/ollama_pilot_test.py: test whether a smaller/cheaper
model can do the Weather agent's actual job against real data, via a real
schema-constrained call. Weather's flag logic is closer to threshold
classification than Fire's open-ended correlation, so this is the fairer
test case for whether local models suit the "simpler" domain agents.

Two backends, same real data, same real schema — for a direct comparison:
  --backend ollama      Local model via Ollama (default)
  --backend openrouter  Hosted model via OpenRouter (needs OPENROUTER_API_KEY)

Usage:
  python3 ollama_pilot_test.py
  python3 ollama_pilot_test.py --model qwen3.5:4b
  python3 ollama_pilot_test.py --model qwen3.5:4b --think
  python3 ollama_pilot_test.py --backend openrouter
  python3 ollama_pilot_test.py --backend openrouter --model qwen/qwen3.5-9b
"""

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent / "data" / "weather.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = {
    "ollama": "qwen2.5:3b-instruct-q4_K_M",
    "openrouter": "qwen/qwen3.5-9b",
}

# Same criteria as Weather/agent.py's real SYSTEM_PROMPT
SYSTEM_PROMPT = """You are an autonomous weather monitoring agent for Napa County, California.
You run on a schedule with no human present. Your focus is on conditions relevant to:
  - Fire weather risk (temperature, humidity, wind, recent precipitation)
  - Flood/precipitation risk (rainfall amounts, trends)
  - Any active NWS watches or warnings

Fire weather flag criteria (flag if ANY are true):
- Active Red Flag Warning or Fire Weather Watch
- Temperature >= 90F AND humidity <= 25% AND wind >= 15 mph
- Humidity <= 15% regardless of other factors
- Wind gusts >= 45 mph

Flood flag criteria:
- Active Flood Watch or Warning
- Precipitation > 25mm in 1 hour
- Precipitation > 50mm in 24 hours

Be specific about values. Reference actual F, %, mph readings.
Note wind direction — offshore (NE/E) winds in Napa are Diablo winds and especially dangerous for fire.
"""

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

    latest = conn.execute("SELECT * FROM observations ORDER BY collected_at DESC LIMIT 1").fetchone()

    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stats = conn.execute(
        """
        SELECT COUNT(*) as n_readings, MIN(temperature_f) as min_temp_f,
               AVG(temperature_f) as avg_temp_f, MAX(temperature_f) as max_temp_f,
               MIN(humidity_pct) as min_humidity, AVG(humidity_pct) as avg_humidity,
               MAX(wind_speed_mph) as max_wind_mph, MAX(wind_gust_mph) as max_gust_mph,
               SUM(precip_1h_mm) as total_precip_mm
        FROM observations WHERE collected_at >= ?
        """,
        (cutoff_7d,),
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()
    alerts = conn.execute(
        """
        SELECT event, severity, urgency, headline, onset, expires, zones
        FROM alerts WHERE (expires IS NULL OR expires > ?)
        GROUP BY alert_id ORDER BY collected_at DESC
        """,
        (now,),
    ).fetchall()

    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    trend_48h = conn.execute(
        """
        SELECT MIN(humidity_pct) as min_humidity_48h, AVG(humidity_pct) as avg_humidity_48h,
               MAX(temperature_f) as max_temp_48h, MAX(wind_speed_mph) as max_wind_48h,
               MAX(wind_gust_mph) as max_gust_48h,
               SUM(COALESCE(precip_1h_mm, 0)) as total_precip_48h_mm
        FROM observations WHERE collected_at >= ?
        """,
        (cutoff_48h,),
    ).fetchone()

    obs = conn.execute(
        "SELECT observed_at, summary, flagged FROM agent_observations ORDER BY observed_at DESC LIMIT 3"
    ).fetchall()

    conn.close()

    sections = [
        "=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n" + json.dumps([dict(r) for r in obs], indent=2),
        "=== CURRENT CONDITIONS ===\n" + json.dumps(
            {"current": dict(latest) if latest else {}, "seven_day_stats": dict(stats) if stats else {}}, indent=2
        ),
        "=== ACTIVE NWS ALERTS ===\n" + (
            json.dumps([dict(r) for r in alerts], indent=2) if alerts
            else "No active NWS alerts for Napa County."
        ),
        "=== FIRE RISK INDICATORS (48h trend) ===\n" + json.dumps(dict(trend_48h) if trend_48h else {}, indent=2),
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
                             "trace (off by default)")
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
