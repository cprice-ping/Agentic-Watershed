"""
Fire Agent
----------
Autonomous agent that runs on a schedule (cron or manually).
No conversation, no human in the loop.

Flow per run:
  1. Load prior observations (memory)
  2. Call MCP tools to gather current hotspot data
  3. Send all context to Claude (Haiku by default — cheap, fast)
  4. Parse Claude's structured response
  5. Write observation back to DB via MCP tool

This agent's job is narrower than it might look: it does NOT assess fire
*weather* (that's the Weather agent's Red Flag Warning / wind / humidity
job) and does NOT assess *smoke* (that's AQI's PM2.5/ozone job). It answers
one specific question those two can't: is there an actual satellite-detected
heat source near Napa Valley right now. Synthesis correlates all three.

Usage:
  python agent.py                    # single run
  python agent.py --model sonnet     # use Sonnet for richer reasoning
  python agent.py --dry-run          # reason but don't write observation
  python agent.py --verbose          # print full Claude response

Cron (every 6 hours, offset from the other domain agents):
  0 3,9,15,21 * * * cd /path/to/Fire && python agent.py >> logs/agent.log 2>&1
"""

import argparse
import json
import os
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

SYSTEM_PROMPT = """You are an autonomous fire-detection monitoring agent for Napa Valley, California.
You run on a schedule with no human present. Your job is narrow and specific:

You are NOT assessing fire weather (wind, humidity, Red Flag Warnings — a separate
Weather agent does that). You are NOT assessing smoke or air quality (a separate AQI
agent does that). Your only job: is there an actual satellite-detected heat source
(a "hotspot") near Napa Valley right now, based on NASA FIRMS thermal detections.

1. Check your memory (recent observations) for continuity
2. Check the collector's last poll status — distinguish "collector is healthy
   and there's genuinely nothing new" (status=ok) from "collector is failing"
   (status=error). A quiet hotspot table means different things depending on
   which of these it is — don't conflate them in your summary.
3. Check the nearest current hotspots and how many exist within 50 miles.
   get_nearest_hotspots only returns hotspots within its currency window
   (matching how far back FIRMS itself is queried) — an empty or short list
   there means nothing current nearby, full stop. Don't describe the feed
   itself as "frozen" or "stale" based on hotspot ages; that's answered by
   get_last_poll_status, not by how old the nearest hotspot is.
4. Assess: are there new hotspots since last run? Are any close (<20mi) or
   high-confidence? Is FRP (fire radiative power) rising, indicating a growing fire?
5. Write a clear, concise observation that will inform Synthesis's cross-domain
   reasoning — Synthesis will correlate your findings with Weather's wind direction
   and AQI's smoke signature to determine if a detected hotspot explains observed smoke.

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "1-3 sentence summary of current hotspot status for the next agent run to read",
  "flagged": true or false,
  "reasoning": "Your full reasoning: what data you saw, what it means, why you flagged or didn't"
}

Flag (set flagged=true) if ANY of these are true:
- Any hotspot detected within 20 miles of Napa Valley center — this is
  unconditional on confidence level. A low-confidence detection within 20
  miles still counts; do not require elevated confidence for this specific
  trigger (that requirement only applies to the separate 50-mile rule below).
- Any high-confidence hotspot within 50 miles
- FRP (fire radiative power) rising across consecutive polls for a hotspot in range
- A new hotspot cluster appeared since the last observation that wasn't there before
- The collector's last poll status is "error" — this is a data-quality issue
  worth flagging on its own, distinct from a fire risk finding; say plainly
  that hotspot data could not be refreshed and existing hotspot data may be stale

Persistence exception: if the exact same low-confidence hotspot has already
been observed and flagged in multiple consecutive prior runs, with no new
detections nearby, no FRP escalation, and no change in character, it's
reasonable to stop treating each identical re-observation as newly alarming.
If you do this, say so explicitly in the summary (e.g. "previously-flagged
persistent low-confidence hotspot, unchanged, not re-flagging") — don't
silently downgrade it without explanation. A genuinely new detection within
20 miles always flags, regardless of how many old persistent ones exist.

Be specific about values. Reference actual distances, confidence levels, and FRP.
If no hotspots are detected in range, say so plainly — a clear 'none detected' is
as useful as an alert, especially when correlated against AQI showing smoke with
no identified local source.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fire.agent")


# ---------------------------------------------------------------------------
# MCP client — calls tools by spawning the MCP server as a subprocess
# ---------------------------------------------------------------------------

def call_mcp_tool(tool_name: str, arguments: dict = None) -> str:
    """Call a tool on the fire MCP server via stdio subprocess. Returns the
    tool result as a string."""
    arguments = arguments or {}

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    init_request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fire-agent", "version": "1.0"},
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
        log.debug("MCP server stderr: %s", stderr[:500])
    return f"[Tool call failed: no valid response for {tool_name}]"


# ---------------------------------------------------------------------------
# Agent logic
# ---------------------------------------------------------------------------

def gather_context() -> str:
    """Call MCP tools to build a rich context string for the LLM."""
    log.info("Gathering context from MCP tools...")

    sections = []

    log.info("  → get_recent_observations")
    obs = call_mcp_tool("get_recent_observations", {"n": 3})
    sections.append(f"=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n{obs}")

    log.info("  → get_last_poll_status")
    poll_status = call_mcp_tool("get_last_poll_status", {})
    sections.append(f"=== COLLECTOR STATUS (is the poller itself healthy?) ===\n{poll_status}")

    log.info("  → get_nearest_hotspots")
    nearest = call_mcp_tool("get_nearest_hotspots", {"n": 10})
    sections.append(f"=== NEAREST HOTSPOTS (most recent poll) ===\n{nearest}")

    log.info("  → get_hotspot_count_since (24h, 50mi)")
    count = call_mcp_tool("get_hotspot_count_since", {"hours_ago": 24.0, "max_distance_mi": 50.0})
    sections.append(f"=== HOTSPOT COUNT: LAST 24H WITHIN 50MI ===\n{count}")

    log.info("  → get_hotspots_since (48h)")
    recent = call_mcp_tool("get_hotspots_since", {"hours_ago": 48.0})
    sections.append(f"=== HOTSPOTS: LAST 48 HOURS (nearest first) ===\n{recent}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    context = f"Agent run at: {now}\n\n" + "\n\n".join(sections)
    return context


# Forced tool use — API validates arguments against this schema server-side
# and hands back an already-parsed dict via the tool_use block's .input.
_ASSESSMENT_TOOL = {
    "name": "submit_assessment",
    "description": "Submit the structured assessment for this monitoring run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "1-3 sentence summary of current hotspot status for the next agent run to read",
            },
            "flagged": {
                "type": "boolean",
                "description": "True if a hotspot warrants attention or follow-up",
            },
            "reasoning": {
                "type": "string",
                "description": "Full reasoning: what data you saw, what it means, why you flagged or didn't",
            },
        },
        "required": ["summary", "flagged", "reasoning"],
    },
}


def reason(context: str, model_key: str, verbose: bool = False) -> dict:
    """Send context to Claude and return its structured assessment."""
    model_id = MODELS[model_key]
    log.info("Reasoning with %s (%s)...", model_key, model_id)
    # Record which model actually produced this observation. The MCP server
    # is spawned as a subprocess and inherits this, so the value reaching the
    # published record's `agentModel` field comes from the harness rather than
    # from the model's own say-so.
    os.environ["AGENT_MODEL"] = model_id

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model_id,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
        tools=[_ASSESSMENT_TOOL],
        tool_choice={"type": "tool", "name": "submit_assessment"},
    )

    if verbose:
        log.info("Raw Claude response:\n%s", message.content)

    tool_use = next((b for b in message.content if b.type == "tool_use"), None)
    if tool_use is None:
        log.error("No tool_use block in response despite forced tool_choice: %s", message.content)
        return {
            "summary": "Agent run failed: model did not return a tool call.",
            "flagged": True,
            "reasoning": f"Raw content blocks: {message.content}",
        }
    return tool_use.input


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
    parser = argparse.ArgumentParser(description="Fire autonomous agent")
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        default=DEFAULT_MODEL,
        help="Claude model to use (default: haiku)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write observation")
    parser.add_argument("--verbose", action="store_true", help="Print full Claude response")
    args = parser.parse_args()

    log.info("=== Fire Agent starting ===")
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
