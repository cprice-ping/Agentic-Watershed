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
DB_PATH = Path(__file__).parent / "data" / "watershed.db"

# Shared MCP client and run-outcome recording. At the repo root rather than
# copied per domain: four private copies of this is how the flag thresholds
# drifted, and all four had the same unguarded 30s timeout.
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import (  # noqa: E402
    call_mcp_tool as _call_mcp_tool, record_failed_run, MCPUnavailable,
)

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())

MODELS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
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
    """Delegate to the shared client, which retries once on timeout and
    raises MCPUnavailable rather than letting a bare TimeoutExpired end the
    run with no record of why."""
    return _call_mcp_tool(MCP_SERVER_PATH, tool_name, arguments,
                          client_name="watershed-agent", log=log)


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


# Forced tool use instead of asking for free-text JSON: the API validates
# the arguments against this schema server-side and hands back an already-
# parsed dict via the tool_use block's .input, so there's no text response
# to parse and no possibility of the model prepending prose before its
# answer (the failure mode that used to require _extract_json_object()).
_ASSESSMENT_TOOL = {
    "name": "submit_assessment",
    "description": "Submit the structured assessment for this monitoring run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "1-3 sentence summary of current conditions for the next agent run to read",
            },
            "flagged": {
                "type": "boolean",
                "description": "True if conditions warrant attention or follow-up",
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
        messages=[
            {
                "role": "user",
                "content": context,
            }
        ],
        tools=[_ASSESSMENT_TOOL],
        tool_choice={"type": "tool", "name": "submit_assessment"},
    )

    # Token usage, recorded so the monthly bill can be attributed per domain
    # rather than estimated. Passed to the MCP server the same way the model
    # id is — via the environment, because it is a measurement the harness
    # makes, not something the model reports about itself.
    usage = getattr(message, "usage", None)
    if usage is not None:
        os.environ["AGENT_INPUT_TOKENS"] = str(getattr(usage, "input_tokens", "") or "")
        os.environ["AGENT_OUTPUT_TOKENS"] = str(getattr(usage, "output_tokens", "") or "")
        log.info("Tokens: %s in / %s out",
                 getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))

    if verbose:
        log.info("Raw Claude response:\n%s", message.content)

    tool_use = next((b for b in message.content if b.type == "tool_use"), None)
    if tool_use is None:
        # Shouldn't happen with a forced tool_choice, but don't trust that blindly.
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
    # A crashed run used to leave nothing behind: no row, no record, and a
    # publisher that logged "Nothing new to publish" — the same line it logs
    # for a domain that ran fine and had nothing new. Weather and watershed
    # were absent from synthesis for two days on 2026-09-09/10 and nothing in
    # the system said so. The failure row makes the gap self-describing.
    #
    # SystemExit and KeyboardInterrupt deliberately propagate untouched: an
    # operator stopping a run is not a fault, and --dry-run exits cleanly.
    try:
        main()
    except MCPUnavailable as exc:
        record_failed_run(DB_PATH, str(exc), log=log)
        log.error("=== Agent run FAILED: %s ===", exc)
        sys.exit(1)
    except Exception as exc:
        record_failed_run(DB_PATH, f"{type(exc).__name__}: {exc}", log=log)
        log.exception("=== Agent run FAILED ===")
        sys.exit(1)
