"""
AQI Agent
---------
Autonomous air quality monitoring agent for Napa County.
Primary focus: wildfire smoke detection via PM2.5 spike analysis.

Same architecture as watershed and weather agents.

Usage:
  python agent.py
  python agent.py --model sonnet
  python agent.py --dry-run --verbose

Cron (every 6 hours, offset from other agents):
  0 2,8,14,20 * * * cd /home/cprice/Agentic/AQI && ANTHROPIC_API_KEY=sk-ant-... AIRNOW_API_KEY=... .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
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

MCP_SERVER_PATH = Path(__file__).parent / "mcp_server.py"

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

DEFAULT_MODEL = "haiku"

SYSTEM_PROMPT = """You are an autonomous air quality monitoring agent for Napa County, California.
You run on a schedule with no human present.

Your primary job is to detect wildfire smoke. PM2.5 is your key signal.
A sudden PM2.5 AQI rise — especially without a corresponding ozone rise —
often indicates a nearby fire has started before any other warning system triggers.

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "2-3 sentence summary of current AQI conditions for the next run to read",
  "flagged": true or false,
  "reasoning": "Full reasoning including which values you saw and why you concluded what you did"
}

Flag (set flagged=true) if ANY of these are true:
- PM2.5 AQI ≥ 101 (Unhealthy for Sensitive Groups or worse)
- PM2.5 AQI rising ≥ 20 points in 3 hours
- PM2.5 AQI ≥ 75 AND previous observation was ≤ 50 (sudden jump from Good to elevated Moderate)
- Any category_number ≥ 4 (Unhealthy) for any parameter

When smoke is suspected (PM2.5 spike without ozone rise), say so explicitly.
Always note the AQI category name, not just the number.
Note if conditions are improving (falling AQI) vs deteriorating (rising AQI).
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("aqi.agent")


# ---------------------------------------------------------------------------
# MCP client (same stdio pattern as other agents)
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
            "clientInfo": {"name": "aqi-agent", "version": "1.0"},
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

    log.info("  → get_current_aqi")
    current = call_mcp_tool("get_current_aqi")
    sections.append(f"=== CURRENT AQI (PM2.5 and OZONE) ===\n{current}")

    log.info("  → get_smoke_indicators")
    smoke = call_mcp_tool("get_smoke_indicators")
    sections.append(f"=== SMOKE INDICATORS ===\n{smoke}")

    log.info("  → get_aqi_since (48h)")
    trend = call_mcp_tool("get_aqi_since", {"hours_ago": 48.0})
    sections.append(f"=== AQI READINGS: LAST 48 HOURS ===\n{trend}")

    log.info("  → get_aqi_trend (7d)")
    weekly = call_mcp_tool("get_aqi_trend", {"days": 7})
    sections.append(f"=== DAILY AQI TREND: LAST 7 DAYS ===\n{weekly}")

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
    parser = argparse.ArgumentParser(description="AQI autonomous agent")
    parser.add_argument("--model", choices=list(MODELS.keys()), default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    log.info("=== AQI Agent starting ===")
    log.info("Model: %s  |  Dry run: %s", args.model, args.dry_run)

    context = gather_context()
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Agent conclusion ---")
    log.info("Summary: %s", observation.get("summary", ""))
    log.info("Flagged: %s", observation.get("flagged", False))

    write_observation(observation, dry_run=args.dry_run)
    log.info("=== AQI Agent run complete ===")


if __name__ == "__main__":
    main()
