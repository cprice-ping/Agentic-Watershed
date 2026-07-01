"""
Watershed Agent
---------------
Autonomous agent that runs on a schedule (cron or manually).
No conversation, no human in the loop.

Flow per run:
  1. Load prior observations (memory)
  2. Call MCP tools to gather current data
  3. Send all context to Claude (Haiku by default — cheap, fast)
  4. Parse Claude's structured response
  5. Write observation back to DB via MCP tool

The agent is stateless between runs; its memory is the observations
table it reads and writes through the MCP server.

Usage:
  python agent.py                    # single run
  python agent.py --model sonnet     # use Sonnet for richer reasoning
  python agent.py --dry-run          # reason but don't write observation
  python agent.py --verbose          # print full Claude response

Cron (every 6 hours):
  0 */6 * * * cd /path/to/watershed && python agent/agent.py >> logs/agent.log 2>&1
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

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

DEFAULT_MODEL = "haiku"

SYSTEM_PROMPT = """You are an autonomous watershed monitoring agent for the Napa River.
You run on a schedule with no human present. Your job is to:

1. Check your memory (recent observations) for continuity
2. Assess current gauge readings against recent history
3. Identify anything noteworthy: flood risk, drought conditions, unusual flow patterns,
   rapid changes, or sustained anomalies
4. Write a clear, concise observation that will inform the next agent run

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "1-3 sentence summary of current conditions for the next agent run to read",
  "flagged": true or false,
  "reasoning": "Your full reasoning: what data you saw, what it means, why you flagged or didn't"
}

Be specific about values. Reference actual cfs and ft readings.
If conditions are normal, say so plainly — a clear 'normal' is as useful as an alert.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("watershed.agent")


# ---------------------------------------------------------------------------
# MCP client — calls tools by spawning the MCP server as a subprocess
# using the Anthropic Python SDK's MCP support
# ---------------------------------------------------------------------------

def call_mcp_tool(tool_name: str, arguments: dict = None) -> str:
    """
    Call a tool on the watershed MCP server via stdio subprocess.
    Returns the tool result as a string.
    """
    arguments = arguments or {}

    # Build a minimal JSON-RPC call over stdio
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    # We send an initialize first, then the tool call
    init_request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "watershed-agent", "version": "1.0"},
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

    # Parse the last complete JSON-RPC response
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
        log.debug("MCP server stderr: %s", stderr[:500])
    return f"[Tool call failed: no valid response for {tool_name}]"


# ---------------------------------------------------------------------------
# Agent logic
# ---------------------------------------------------------------------------

def gather_context() -> str:
    """Call MCP tools to build a rich context string for the LLM."""
    log.info("Gathering context from MCP tools...")

    sections = []

    # Memory first
    log.info("  → get_recent_observations")
    obs = call_mcp_tool("get_recent_observations", {"n": 3})
    sections.append(f"=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n{obs}")

    # Current conditions
    for station_id, station_name in _NODE_CFG["watershed"]["usgs_stations"].items():
        log.info("  → get_station_summary (%s)", station_name)
        data = call_mcp_tool("get_station_summary", {"station_id": station_id})
        sections.append(f"=== STATION: {station_name.upper()} ({station_id}) ===\n{data}")

    # Anomaly check
    log.info("  → get_anomalies")
    anomalies = call_mcp_tool("get_anomalies", {"threshold_pct": 40.0})
    sections.append(f"=== ANOMALY SCAN (>40% deviation from 30-day mean) ===\n{anomalies}")

    # Recent trend
    log.info("  → get_readings_since (48h)")
    recent = call_mcp_tool("get_readings_since", {"hours_ago": 48.0})
    sections.append(f"=== READINGS: LAST 48 HOURS ===\n{recent}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    context = f"Agent run at: {now}\n\n" + "\n\n".join(sections)
    return context


def reason(context: str, model_key: str, verbose: bool = False) -> dict:
    """Send context to Claude and parse its structured JSON response."""
    model_id = MODELS[model_key]
    log.info("Reasoning with %s (%s)...", model_key, model_id)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model_id,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": context,
            }
        ],
    )

    raw = message.content[0].text.strip()

    if verbose:
        log.info("Raw Claude response:\n%s", raw)

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Failed to parse Claude response as JSON: %s", exc)
        log.error("Raw response: %s", raw)
        return {
            "summary": "Agent run failed: could not parse LLM response.",
            "flagged": True,
            "reasoning": f"JSON parse error: {exc}\nRaw: {raw}",
        }


def write_observation(observation: dict, dry_run: bool = False) -> None:
    """Write the agent's conclusion back to DB via MCP tool."""
    if dry_run:
        log.info("[DRY RUN] Would write observation:")
        log.info("  Summary: %s", observation["summary"])
        log.info("  Flagged: %s", observation["flagged"])
        return

    log.info("Writing observation to DB...")
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
    parser = argparse.ArgumentParser(description="Watershed autonomous agent")
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        default=DEFAULT_MODEL,
        help="Claude model to use (default: haiku)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write observation")
    parser.add_argument("--verbose", action="store_true", help="Print full Claude response")
    args = parser.parse_args()

    log.info("=== Watershed Agent starting ===")
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
