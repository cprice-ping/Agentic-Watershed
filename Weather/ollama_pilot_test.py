"""
Throwaway pilot script — NOT wired into agent.py.
Same purpose as Fire/ollama_pilot_test.py: test whether a local Ollama
model can do the Weather agent's actual job against real data, via a
real schema-constrained call. Weather's flag logic is closer to threshold
classification than Fire's open-ended correlation, so this is the fairer
test case for whether local models suit the "simpler" domain agents.

Usage (on the Pi):
  python3 ollama_pilot_test.py
  python3 ollama_pilot_test.py --model qwen3.5:4b
  python3 ollama_pilot_test.py --model qwen3.5:4b --think
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent / "data" / "weather.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:3b-instruct-q4_K_M"

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Ollama model tag to test (default: {DEFAULT_MODEL})")
    parser.add_argument("--think", action="store_true",
                        help="Allow reasoning-capable models to emit a thinking trace (off by default)")
    args = parser.parse_args()

    context = gather_real_context()
    print(f"--- Model: {args.model}  |  think={args.think}  |  Context size: {len(context)} chars ---\n")

    payload = {
        "model": args.model,
        "system": SYSTEM_PROMPT,
        "prompt": context,
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "think": args.think,
    }

    start = time.monotonic()
    resp = httpx.post(OLLAMA_URL, json=payload, timeout=600)
    resp.raise_for_status()
    elapsed = time.monotonic() - start

    result = resp.json()
    print(f"--- Raw response object ({elapsed:.1f}s) ---")
    print(json.dumps(result, indent=2)[:3000])

    print(f"\n--- response field ---")
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
