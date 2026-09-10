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
import os
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import anthropic

# Flag criteria come from thresholds.py, the same module mcp_server.py and
# flag_rules.py read, so the rules stated in the prompt below are generated
# rather than transcribed. They used to be transcribed, and they drifted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import thresholds  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MCP_SERVER_PATH = Path(__file__).parent / "mcp_server.py"
DB_PATH = Path(__file__).parent / "data" / "weather.db"

# Shared MCP client and run-outcome recording. At the repo root rather than
# copied per domain: four private copies of this is how the flag thresholds
# drifted, and all four had the same unguarded 30s timeout.
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import (  # noqa: E402
    call_mcp_tool as _call_mcp_tool, record_failed_run, MCPUnavailable,
)

MODELS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-6",
}

DEFAULT_MODEL = "haiku"

_SYSTEM_PROMPT_TEMPLATE = """You are an autonomous weather monitoring agent for Napa County, California.
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

Fire weather flag criteria — these apply if met at ANY point in the current
reading OR the $trend_hours-hour trend data, not only the current instantaneous
reading. A calm current moment during an ongoing extreme-weather event
(e.g. a lull in sustained high winds) still warrants a flag if the
$trend_hours-hour trend shows the threshold was crossed — conditions don't stop being
dangerous just because this exact instant is quieter than the last few
hours (flag if ANY are true):
$fire_criteria

Flood flag criteria (also current-or-trend, same reasoning as above):
$flood_criteria

Be specific about values. Reference actual °F, %, mph readings.
Note wind direction — offshore (NE/E) winds in Napa are Diablo winds and especially dangerous for fire.
"""

# string.Template rather than .format() or an f-string: the prompt contains a
# literal JSON example, and brace-based substitution would collide with it.
SYSTEM_PROMPT = Template(_SYSTEM_PROMPT_TEMPLATE).substitute(
    trend_hours=int(thresholds.TREND_WINDOW_HOURS),
    fire_criteria=thresholds.fire_criteria_text(),
    flood_criteria=thresholds.flood_criteria_text(),
)

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
    """Delegate to the shared client, which retries once on timeout and
    raises MCPUnavailable rather than letting a bare TimeoutExpired end the
    run with no record of why."""
    return _call_mcp_tool(MCP_SERVER_PATH, tool_name, arguments,
                          client_name="weather-agent", log=log)


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
                "description": "2-3 sentence summary of current conditions for the next agent run to read",
            },
            "flagged": {
                "type": "boolean",
                "description": "True if conditions warrant attention or follow-up",
            },
            "reasoning": {
                "type": "string",
                "description": "Full reasoning: what data you saw, what thresholds were considered, why flagged or not",
            },
        },
        "required": ["summary", "flagged", "reasoning"],
    },
}


def reason(context: str, model_key: str, verbose: bool = False) -> dict:
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
        log.error("No tool_use block in response despite forced tool_choice: %s", message.content)
        return {
            "summary": "Agent run failed: model did not return a tool call.",
            "flagged": True,
            "reasoning": f"Raw content blocks: {message.content}",
        }
    return tool_use.input


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
