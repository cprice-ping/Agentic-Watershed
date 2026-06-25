"""
Weather Agent
-------------
Autonomous weather monitoring agent for Napa County.
Focuses on fire weather risk and precipitation patterns.

Identical architecture to the watershed agent:
  - Cron triggered, stateless between runs
  - Reads memory from previous agent_observations
  - Calls MCP tools for current data
  - Reasons with Claude
  - Writes structured observation back to DB

Usage:
  python agent.py
  python agent.py --model sonnet
  python agent.py --dry-run --verbose

Cron (every 6 hours):
  0 */6 * * * cd /home/cprice/Agentic/Weather && ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MCP_SERVER_PATH = Path(__file__).parent.parent / "mcp_server" / "mcp_server.py"

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

DEFAULT_MODEL = "haiku"

SYSTEM_PROMPT = """You are an autonomous weather monitoring agent for Napa County, California.
You run on a schedule with no human present. Your focus is on conditions relevant to:
  - Fire weather risk (temperature, humidity, wind, recent precipitation)
  - Flood/precipitation risk (rainfall amounts, trends)
  - Any active NWS watches or warnings

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "2-3 sentence summary of current conditions for the next agent run to read",
  "flagged": true or false,
  "reasoning": "Full reasoning: what data you saw, what thresholds were considered, why flagged or not"
}

Fire weather flag criteria (flag if ANY are true):
- Active Red Flag Warning or Fire Weather Watch
- Temperature ≥ 90°F AND humidity ≤ 25% AND wind ≥ 15 mph
- Humidity ≤ 15% regardless of other factors
- Wind gusts ≥ 45 mph

Flood flag criteria:
- Active Flood Watch or Warning
- Precipitation > 25mm in 1 hour
- Precipitation > 50mm in 24 hours

Be specific about values. Reference actual °F, %, mph readings.
Note wind direction — offshore (NE/E) winds in Napa are Diablo winds and especially dangerous for fire.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("weather.agent")


# ---------------------------------------------------------------------------
# MCP client (same stdio pattern as watershed agent)
# ---------------------------------------------------------------------------

def call_mcp_tool(tool_name: str, arguments: dict = None) -> str:
    arguments = arguments or {}

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    init_request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "weather-agent", "version": "1.0"},
        },
    }

    proc = subprocess.Popen(
        [sys.executable, str(MCP_SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdin_data = (
        json.dumps(init_request) + "\n" +
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n" +
        json.dumps(request) + "\n"
    )

    stdout, stderr = proc.communicate(stdin_data, timeout=30)

    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            response = json.loads(line)
            if response.get("id") == 1:
                result = response.get("result", {})
                content = result.get("content", [])
                if content:
                    return content[0].get("text", "")
        except json.JSONDecodeError:
            continue

    if stderr:
        log.debug("MCP stderr: %s", stderr[:500])
    return f"[Tool call failed: {tool_name}]"


# ---------------------------------------------------------------------------
# Agent logic
# ---------------------------------------------------------------------------

def gather_context() -> str:
    log.info("Gathering context from MCP tools...")
    sections = []

    log.info("  → get_recent_agent_observations")
    obs = call_mcp_tool("get_recent_agent_observations", {"n": 3})
    sections.append(f"=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n{obs}")

    log.info("  → get_current_conditions")
    current = call_mcp_tool("get_current_conditions")
    sections.append(f"=== CURRENT CONDITIONS (KAPC — Napa County Airport) ===\n{current}")

    log.info("  → get_active_alerts")
    alerts = call_mcp_tool("get_active_alerts")
    sections.append(f"=== ACTIVE NWS ALERTS ===\n{alerts}")

    log.info("  → get_fire_risk_indicators")
    fire = call_mcp_tool("get_fire_risk_indicators")
    sections.append(f"=== FIRE RISK INDICATORS ===\n{fire}")

    log.info("  → get_observations_since (48h)")
    trend = call_mcp_tool("get_observations_since", {"hours_ago": 48.0})
    sections.append(f"=== OBSERVATIONS: LAST 48 HOURS ===\n{trend}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"Agent run at: {now}\n\n" + "\n\n".join(sections)


def reason(context: str, model_key: str, verbose: bool = False) -> dict:
    model_id = MODELS[model_key]
    log.info("Reasoning with %s (%s)...", model_key, model_id)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model_id,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )

    raw = message.content[0].text.strip()
    if verbose:
        log.info("Raw Claude response:\n%s", raw)

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Failed to parse response: %s", exc)
        return {
            "summary": "Agent run failed: could not parse LLM response.",
            "flagged": True,
            "reasoning": f"JSON parse error: {exc}\nRaw: {raw}",
        }


def write_observation(observation: dict, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Would write observation:")
        log.info("  Summary: %s", observation["summary"])
        log.info("  Flagged: %s", observation["flagged"])
        return

    result = call_mcp_tool(
        "write_agent_observation",
        {
            "summary": observation["summary"],
            "flagged": observation.get("flagged", False),
            "reasoning": observation.get("reasoning", ""),
        },
    )
    log.info("Observation written: %s", result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Weather autonomous agent")
    parser.add_argument("--model", choices=list(MODELS.keys()), default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    log.info("=== Weather Agent starting ===")
    log.info("Model: %s  |  Dry run: %s", args.model, args.dry_run)

    context = gather_context()
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Agent conclusion ---")
    log.info("Summary: %s", observation.get("summary", ""))
    log.info("Flagged: %s", observation.get("flagged", False))

    write_observation(observation, dry_run=args.dry_run)
    log.info("=== Agent run complete ===")


if __name__ == "__main__":
    main()
