"""
Throwaway pilot script — NOT wired into agent.py.
Tests whether a local Ollama model can do the Fire agent's actual job:
read real recent hotspot/observation data, produce a structured verdict,
via a real schema-constrained call (not just "please respond in JSON").

Usage (on the Pi, after `ollama pull qwen2.5:3b-instruct-q4_K_M`):
  python3 ollama_pilot_test.py
"""

import json
import sqlite3
import time
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent / "data" / "fire.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:3b-instruct-q4_K_M"

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


def main() -> None:
    context = gather_real_context()
    print(f"--- Context size: {len(context)} chars ---\n")

    payload = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": context,
        "format": RESPONSE_SCHEMA,
        "stream": False,
    }

    start = time.monotonic()
    resp = httpx.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    elapsed = time.monotonic() - start

    result = resp.json()
    print(f"--- Response ({elapsed:.1f}s) ---")
    print(result.get("response", "<no response field>"))

    try:
        parsed = json.loads(result["response"])
        print("\n--- Parsed OK ---")
        print(f"flagged: {parsed.get('flagged')}")
        print(f"summary: {parsed.get('summary')}")
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"\n--- FAILED TO PARSE: {exc} ---")


if __name__ == "__main__":
    main()
