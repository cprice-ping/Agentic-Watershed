"""
Synthesis Agent (ATProto version)
----------------------------------
Cross-domain risk assessment agent that reads observations from the
ATProto subscriber database rather than domain SQLite files directly.

This is the distributed version of synthesis/agent.py:
  - Before: reads SQLite files from domain agent stacks (filesystem coupling)
  - After: reads from subscriber.db populated by the firehose subscriber

The synthesis reasoning logic is identical — what changes is the data source.
This agent can run anywhere that has access to subscriber.db, which means
it can move off the Pi to a separate machine without changing the reasoning.

Usage:
  python agent_atproto.py
  python agent_atproto.py --model sonnet
  python agent_atproto.py --dry-run --verbose
  python agent_atproto.py --lookback 48   # hours of observations to consider

Cron:
  0 6,18 * * * . /etc/environment && cd /home/cprice/Agentic/Synthesis && .venv/bin/python agent_atproto.py >> logs/agent_atproto.log 2>&1
"""

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Subscriber DB — populated by the firehose subscriber
SUBSCRIBER_DB = Path(__file__).parent.parent / "ATProto" / "data" / "subscriber.db"

# Synthesis output DB — same as before
SYNTHESIS_DB = Path(__file__).parent / "data" / "synthesis.db"

MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}

DEFAULT_MODEL = "sonnet"

SYSTEM_PROMPT = """You are a cross-domain environmental risk assessment agent for Napa Valley, California.

You receive concluded observations from autonomous monitoring nodes, published via ATProto
and collected from the firehose. Each observation was produced by a domain agent running
on a trusted node and includes a summary, flagged status, and observation type.

Your job is to reason ACROSS domains to produce a unified risk picture.

FIRE RISK compounds when:
  - Weather: high temp (≥90°F), low humidity (≤25%), wind ≥15mph, especially NE/E (Diablo winds)
  - AQI: PM2.5 rising or elevated — may indicate fire already started upwind
  - Watershed: low river flow (summer base conditions reduce firefighting water availability)

FLOOD RISK compounds when:
  - Watershed: rising gage height or flow rate
  - Weather: active precipitation or flood watch active

SMOKE is primarily AQI but wind direction matters:
  - NE/E winds + PM2.5 spike = smoke from interior, act now
  - SW winds + PM2.5 = coastal aerosol, likely transient

Note: observations come from ATProto records published by nodes with verified DIDs.
The trustedPublishers field tells you which nodes contributed.

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "3-4 sentence cross-domain summary suitable for a public Bluesky post",
  "fire_risk": "none|low|moderate|high|extreme",
  "flood_risk": "none|low|moderate|high|extreme",
  "air_quality_risk": "none|low|moderate|high|extreme",
  "overall_risk": "none|low|moderate|high|extreme",
  "flagged": true or false,
  "flag_reason": "brief reason if flagged, empty string if not",
  "reasoning": "Full cross-domain reasoning"
}

flagged=true if overall_risk is moderate or higher, OR if any single domain is high/extreme.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("synthesis.agent_atproto")


# ---------------------------------------------------------------------------
# Read from subscriber DB
# ---------------------------------------------------------------------------

def read_recent_observations(lookback_hours: float = 24.0) -> dict[str, list[dict]]:
    """
    Read recent observations from the subscriber DB, grouped by type.
    Returns dict of {observation_type: [records]} sorted newest first.
    """
    if not SUBSCRIBER_DB.exists():
        log.warning("Subscriber DB not found at %s", SUBSCRIBER_DB)
        log.warning("Is the firehose subscriber running?")
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()

    try:
        conn = sqlite3.connect(SUBSCRIBER_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT observation_type, publisher_did, node_id,
                   observed_at, received_at, summary, flagged,
                   flag_reason, agent_model, raw_record
            FROM observations
            WHERE received_at >= ?
            ORDER BY observation_type, received_at DESC
            """,
            (cutoff,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        log.error("Failed to read subscriber DB: %s", exc)
        return {}

    grouped = {}
    for row in rows:
        obs_type = row["observation_type"] or "unknown"
        if obs_type not in grouped:
            grouped[obs_type] = []
        grouped[obs_type].append(dict(row))

    return grouped


def read_recent_synthesis(n: int = 3) -> list[dict]:
    """Read recent synthesis observations for memory/continuity."""
    if not SYNTHESIS_DB.exists():
        return []
    try:
        conn = sqlite3.connect(SYNTHESIS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT observed_at, summary, fire_risk, flood_risk,
                   air_quality_risk, overall_risk, flagged, flag_reason
            FROM synthesis_observations
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def gather_context(lookback_hours: float = 24.0) -> str:
    log.info("Reading observations from subscriber DB (last %.0fh)...", lookback_hours)

    sections = []

    # Memory
    prior = read_recent_synthesis(3)
    if prior:
        sections.append(
            f"=== PREVIOUS SYNTHESIS OBSERVATIONS (memory) ===\n"
            f"{json.dumps(prior, indent=2)}"
        )
    else:
        sections.append("=== PREVIOUS SYNTHESIS OBSERVATIONS ===\nNone yet.")

    # Domain observations from ATProto
    grouped = read_recent_observations(lookback_hours)

    if not grouped:
        sections.append(
            "=== NODE OBSERVATIONS ===\n"
            "No observations received from the firehose yet.\n"
            "Check that the subscriber daemon is running and nodes are publishing."
        )
    else:
        for obs_type, records in grouped.items():
            log.info("  %s: %d observation(s)", obs_type, len(records))

            # Show publisher provenance
            publishers = list({r["node_id"] for r in records})
            section = (
                f"=== {obs_type.upper()} OBSERVATIONS "
                f"(from: {', '.join(publishers)}) ===\n"
            )

            # Include up to 5 most recent per type
            for r in records[:5]:
                raw = json.loads(r["raw_record"]) if r["raw_record"] else {}
                section += json.dumps({
                    "observed_at": r["observed_at"],
                    "node": r["node_id"],
                    "publisher_did": r["publisher_did"],
                    "summary": r["summary"],
                    "flagged": bool(r["flagged"]),
                    "agent_model": r["agent_model"],
                    # Include domain-specific fields if present
                    **{k: v for k, v in raw.items()
                       if k in ("watershed", "weather", "aqi") and v},
                }, indent=2) + "\n"

            sections.append(section)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    location_context = (
        "Location: Napa Valley, California. "
        "Fire season June–November. Flood risk December–March. "
        "Diablo winds (NE/E offshore) dramatically increase fire risk in autumn."
    )

    return (
        f"Synthesis run at: {now}\n"
        f"{location_context}\n\n"
        + "\n\n".join(sections)
    )


# ---------------------------------------------------------------------------
# Reasoning and output (same as original synthesis agent)
# ---------------------------------------------------------------------------

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
            "summary": "Synthesis agent run failed: could not parse LLM response.",
            "fire_risk": "none", "flood_risk": "none",
            "air_quality_risk": "none", "overall_risk": "none",
            "flagged": True, "flag_reason": f"Parse error: {exc}",
            "reasoning": raw,
        }


def write_observation(obs: dict, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Would write synthesis observation:")
        log.info("  Summary:      %s", obs.get("summary", ""))
        log.info("  Overall risk: %s", obs.get("overall_risk", ""))
        log.info("  Flagged:      %s", obs.get("flagged", False))
        return

    SYNTHESIS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SYNTHESIS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synthesis_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            summary TEXT NOT NULL,
            fire_risk TEXT, flood_risk TEXT,
            air_quality_risk TEXT, overall_risk TEXT,
            flagged INTEGER NOT NULL DEFAULT 0,
            flag_reason TEXT, reasoning TEXT
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO synthesis_observations (
            observed_at, summary, fire_risk, flood_risk,
            air_quality_risk, overall_risk, flagged, flag_reason, reasoning
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now, obs.get("summary", ""),
            obs.get("fire_risk", "none"), obs.get("flood_risk", "none"),
            obs.get("air_quality_risk", "none"), obs.get("overall_risk", "none"),
            int(obs.get("flagged", False)),
            obs.get("flag_reason", ""), obs.get("reasoning", ""),
        ),
    )
    conn.commit()
    conn.close()
    log.info("Synthesis observation written to %s", SYNTHESIS_DB)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesis agent (ATProto version)")
    parser.add_argument("--model", choices=list(MODELS.keys()), default=DEFAULT_MODEL)
    parser.add_argument("--lookback", type=float, default=24.0,
                        help="Hours of observations to consider (default 24)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    log.info("=== Synthesis Agent (ATProto) starting ===")
    log.info("Model: %s  |  Dry run: %s  |  Lookback: %.0fh",
             args.model, args.dry_run, args.lookback)

    context = gather_context(lookback_hours=args.lookback)
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Synthesis conclusion ---")
    log.info("Fire risk:    %s", observation.get("fire_risk", ""))
    log.info("Flood risk:   %s", observation.get("flood_risk", ""))
    log.info("AQI risk:     %s", observation.get("air_quality_risk", ""))
    log.info("Overall risk: %s", observation.get("overall_risk", ""))
    log.info("Flagged:      %s", observation.get("flagged", False))
    log.info("Summary: %s", observation.get("summary", ""))

    write_observation(observation, dry_run=args.dry_run)
    log.info("=== Synthesis Agent (ATProto) run complete ===")


if __name__ == "__main__":
    main()
