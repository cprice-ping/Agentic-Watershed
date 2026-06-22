"""
Synthesis Agent
---------------
Cross-domain risk assessment for Napa Valley.
Reads concluded observations from all three domain agents and reasons
across them to produce a unified risk picture.

No collector. No MCP server. Reads SQLite directly.

Domain agents write conclusions to their own databases.
This agent reads those conclusions — not raw sensor data.

Risk domains:
  - Fire risk (weather + AQI + watershed)
  - Flood risk (watershed + weather)
  - Air quality advisories (AQI alone)
  - Combined/compounding conditions

Output: a synthesis observation written to its own DB,
plus (later) a Bluesky post when flagged.

Usage:
  python agent.py
  python agent.py --model sonnet     # recommended for synthesis
  python agent.py --dry-run --verbose
  python agent.py --observations 5   # how many past obs to read per domain

Cron (twice daily — synthesis doesn't need to run every 6 hours):
  0 6,18 * * * cd /home/cprice/Agentic/Synthesis && ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python agent/agent.py >> logs/agent.log 2>&1
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

BASE = Path("/home/cprice/Agentic")

DB_PATHS = {
    "watershed": BASE / "Watershed" / "data" / "watershed.db",
    "weather":   BASE / "Weather"   / "data" / "weather.db",
    "aqi":       BASE / "AQI"       / "data" / "aqi.db",
}

SYNTHESIS_DB = Path(__file__).parent.parent / "data" / "synthesis.db"

MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}

DEFAULT_MODEL = "sonnet"  # Synthesis warrants more reasoning than domain agents

SYSTEM_PROMPT = """You are a cross-domain environmental risk assessment agent for Napa Valley, California.

You receive concluded observations from three specialist domain agents:
  - Watershed agent: Napa River flow, gage height, flood risk
  - Weather agent: temperature, humidity, wind, NWS alerts
  - AQI agent: PM2.5 and ozone levels, smoke detection

Your job is to reason ACROSS these domains, not just summarise each one.
The interesting signal is in the intersections:

FIRE RISK compounds when:
  - Weather: high temp (≥90°F), low humidity (≤25%), wind ≥15mph, especially NE/E (Diablo winds)
  - AQI: PM2.5 rising or elevated — may indicate fire already started upwind
  - Watershed: low river flow (summer base conditions reduce firefighting water availability)
  All three pointing the same direction = elevated fire risk corridor

FLOOD RISK compounds when:
  - Watershed: rising gage height or flow rate
  - Weather: active precipitation, saturated ground, or flood watch active
  Both together = act; either alone = monitor

SMOKE/AIR QUALITY is primarily AQI but weather wind direction matters:
  - NE/E winds + PM2.5 spike = smoke likely from interior, act now
  - SW winds + PM2.5 = smoke from coast, may be transient

Note when domain agents disagree or have gaps — a missing observation is itself signal.
Note when conditions are improving vs deteriorating across all domains.

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "3-4 sentence cross-domain summary suitable for a public Bluesky post",
  "fire_risk": "none|low|moderate|high|extreme",
  "flood_risk": "none|low|moderate|high|extreme",
  "air_quality_risk": "none|low|moderate|high|extreme",
  "overall_risk": "none|low|moderate|high|extreme",
  "flagged": true or false,
  "flag_reason": "brief reason if flagged, empty string if not",
  "reasoning": "Full cross-domain reasoning — what each agent reported, how they interact, what the combined picture means"
}

flagged=true if overall_risk is moderate or higher, OR if any single domain is high/extreme.
The summary field will be used directly in a Bluesky post — write it for a general Napa Valley audience,
not for specialists. Plain language, specific values, actionable if warranted.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("synthesis.agent")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_synthesis_db() -> sqlite3.Connection:
    SYNTHESIS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SYNTHESIS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS synthesis_observations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at         TEXT NOT NULL,
            summary             TEXT NOT NULL,
            fire_risk           TEXT,
            flood_risk          TEXT,
            air_quality_risk    TEXT,
            overall_risk        TEXT,
            flagged             INTEGER NOT NULL DEFAULT 0,
            flag_reason         TEXT,
            reasoning           TEXT
        );
    """)
    conn.commit()
    return conn


def read_domain_observations(domain: str, n: int = 5) -> list[dict]:
    """Read the most recent N agent observations from a domain database."""
    db_path = DB_PATHS[domain]
    if not db_path.exists():
        log.warning("Database not found for domain '%s': %s", domain, db_path)
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT observed_at, summary, flagged, reasoning
            FROM agent_observations
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.error("Failed to read %s database: %s", domain, exc)
        return []


def read_recent_synthesis(n: int = 3) -> list[dict]:
    """Read recent synthesis observations for continuity."""
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

def gather_context(n_observations: int = 5) -> str:
    log.info("Reading domain agent observations...")
    sections = []

    # Synthesis memory first
    prior = read_recent_synthesis(3)
    if prior:
        sections.append(
            f"=== PREVIOUS SYNTHESIS OBSERVATIONS (memory) ===\n{json.dumps(prior, indent=2)}"
        )
    else:
        sections.append("=== PREVIOUS SYNTHESIS OBSERVATIONS ===\nNone yet — first run.")

    # Domain agent conclusions
    for domain in ["watershed", "weather", "aqi"]:
        obs = read_domain_observations(domain, n_observations)
        label = domain.upper()
        if obs:
            log.info("  %s: %d observation(s) found", domain, len(obs))
            sections.append(
                f"=== {label} AGENT OBSERVATIONS (most recent first) ===\n"
                f"{json.dumps(obs, indent=2)}"
            )
        else:
            log.warning("  %s: no observations found", domain)
            sections.append(
                f"=== {label} AGENT OBSERVATIONS ===\n"
                f"No observations available. Domain agent may not have run yet "
                f"or database is missing at {DB_PATHS[domain]}."
            )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    location_context = (
        "Location context: Napa Valley, California. "
        "Fire season typically June–November. "
        "Flood risk highest December–March. "
        "Diablo winds (NE/E offshore flow) dramatically increase fire risk in autumn."
    )

    return (
        f"Synthesis run at: {now}\n"
        f"{location_context}\n\n"
        + "\n\n".join(sections)
    )


# ---------------------------------------------------------------------------
# Reasoning
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
            "fire_risk": "none",
            "flood_risk": "none",
            "air_quality_risk": "none",
            "overall_risk": "none",
            "flagged": True,
            "flag_reason": f"Parse error: {exc}",
            "reasoning": raw,
        }


# ---------------------------------------------------------------------------
# Write observation
# ---------------------------------------------------------------------------

def write_observation(conn: sqlite3.Connection, obs: dict, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Would write synthesis observation:")
        log.info("  Summary:      %s", obs.get("summary", ""))
        log.info("  Fire risk:    %s", obs.get("fire_risk", ""))
        log.info("  Flood risk:   %s", obs.get("flood_risk", ""))
        log.info("  AQI risk:     %s", obs.get("air_quality_risk", ""))
        log.info("  Overall risk: %s", obs.get("overall_risk", ""))
        log.info("  Flagged:      %s — %s", obs.get("flagged", False), obs.get("flag_reason", ""))
        return

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO synthesis_observations (
            observed_at, summary, fire_risk, flood_risk,
            air_quality_risk, overall_risk, flagged, flag_reason, reasoning
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            obs.get("summary", ""),
            obs.get("fire_risk", "none"),
            obs.get("flood_risk", "none"),
            obs.get("air_quality_risk", "none"),
            obs.get("overall_risk", "none"),
            int(obs.get("flagged", False)),
            obs.get("flag_reason", ""),
            obs.get("reasoning", ""),
        ),
    )
    conn.commit()
    log.info("Synthesis observation written")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesis agent")
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        default=DEFAULT_MODEL,
        help="Claude model to use (default: sonnet)",
    )
    parser.add_argument(
        "--observations", "-n",
        type=int,
        default=5,
        help="Number of past domain observations to read per domain (default: 5)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    log.info("=== Synthesis Agent starting ===")
    log.info("Model: %s  |  Dry run: %s", args.model, args.dry_run)

    conn = init_synthesis_db()
    context = gather_context(n_observations=args.observations)
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Synthesis conclusion ---")
    log.info("Fire risk:    %s", observation.get("fire_risk", ""))
    log.info("Flood risk:   %s", observation.get("flood_risk", ""))
    log.info("AQI risk:     %s", observation.get("air_quality_risk", ""))
    log.info("Overall risk: %s", observation.get("overall_risk", ""))
    log.info("Flagged:      %s", observation.get("flagged", False))
    if observation.get("flag_reason"):
        log.info("Flag reason:  %s", observation.get("flag_reason", ""))
    log.info("Summary: %s", observation.get("summary", ""))

    write_observation(conn, observation, dry_run=args.dry_run)
    log.info("=== Synthesis Agent run complete ===")


if __name__ == "__main__":
    main()
