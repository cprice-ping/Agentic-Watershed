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
from typing import Optional

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Default paths — overridable via CLI args for laptop / remote use.
# Pi layout:   /Agentic/ATProto/data/subscriber.db  (subscriber.py deployed separately)
# Laptop/repo: Synthesis/data/subscriber.db          (subscriber.py runs in-tree)
_DEFAULT_SUBSCRIBER_DB = Path(__file__).parent.parent.parent / "ATProto" / "data" / "subscriber.db"
_DEFAULT_SYNTHESIS_DB  = Path(__file__).parent / "data" / "synthesis.db"

MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}

DEFAULT_MODEL = "sonnet"

SYSTEM_PROMPT = """You are a cross-domain environmental risk assessment agent for Napa Valley, California.

You receive:
  1. A seasonal calendar showing where we are in the fire/flood year and key upcoming transitions
  2. Your own recent synthesis history (memory) — previous risk assessments to reason about trends
  3. Current domain observations from autonomous monitoring nodes (watershed, weather, AQI)
     published via ATProto by trusted nodes with verified DIDs

Your job is to reason ACROSS domains and ACROSS time to produce a unified risk picture.
Don't just assess current conditions — assess trajectory. Are conditions improving or
deteriorating? Are we approaching a known risk window? How does today compare to recent days?

FIRE RISK compounds when:
  - Weather: high temp (≥90°F), low humidity (≤25%), wind ≥15mph, especially NE/E (Diablo winds)
  - AQI: PM2.5 rising or elevated — may indicate fire already started upwind
  - Watershed: low river flow (summer base conditions reduce firefighting water availability)
  Diablo winds (NE/E offshore flow) are the dominant risk multiplier in autumn. Even moderate
  temps with NE winds warrant elevated vigilance during September–November.

FLOOD RISK compounds when:
  - Watershed: rising gage height or flow rate, especially rapid rise
  - Weather: active precipitation, saturated ground, or atmospheric river event

SMOKE is primarily AQI but wind direction matters:
  - NE/E winds + PM2.5 spike = smoke from interior (fire nearby), act now
  - SW winds + PM2.5 = coastal aerosol, likely transient

TRAJECTORY SIGNALS to look for across your recent history:
  - Gradual drying trend in humidity over multiple days = fire risk building
  - AQI creeping up over several runs = smoke accumulating, monitor
  - River flow declining faster than seasonal baseline = drought stress developing
  - Wind direction shifting from SW/W to NE/E = offshore flow pattern developing

You must respond in this exact JSON format (no markdown, no extra text):
{
  "summary": "3-4 sentence cross-domain summary suitable for a public Bluesky post",
  "fire_risk": "none|low|moderate|high|extreme",
  "flood_risk": "none|low|moderate|high|extreme",
  "air_quality_risk": "none|low|moderate|high|extreme",
  "overall_risk": "none|low|moderate|high|extreme",
  "flagged": true or false,
  "flag_reason": "brief reason if flagged, empty string if not",
  "reasoning": "Full cross-domain reasoning including trajectory assessment and seasonal context"
}

flagged=true if overall_risk is moderate or higher, OR if any single domain is high/extreme,
OR if a concerning trajectory is developing even if current conditions are still benign.
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

def read_recent_observations(lookback_hours: float = 24.0,
                             subscriber_db: Path = _DEFAULT_SUBSCRIBER_DB) -> dict[str, list[dict]]:
    """
    Read recent observations from the subscriber DB, grouped by type.
    Returns dict of {observation_type: [records]} sorted newest first.
    """
    if not subscriber_db.exists():
        log.warning("Subscriber DB not found at %s", subscriber_db)
        log.warning("Is the firehose subscriber running?")
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()

    try:
        conn = sqlite3.connect(subscriber_db)
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


def read_recent_synthesis(n: int = 14,
                          synthesis_db: Path = _DEFAULT_SYNTHESIS_DB) -> list[dict]:
    """Read recent synthesis observations for memory/continuity.

    Default 14 = 7 days of twice-daily runs — enough to see weekly drift
    and detect slow-moving trends (gradual drying, creeping AQI, etc.).
    """
    if not synthesis_db.exists():
        return []
    try:
        conn = sqlite3.connect(synthesis_db)
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


def seasonal_context() -> str:
    """Return a dynamic seasonal calendar for Napa Valley based on the current date.

    Gives the agent explicit awareness of where we are in the fire/flood year
    and how far we are from key risk transitions — without relying on the model
    to know the current date from training data.
    """
    now = datetime.now(timezone.utc)
    doy = now.timetuple().tm_yday  # 1-365
    date_str = now.strftime("%B %d, %Y")

    # Approximate day-of-year boundaries (non-leap year)
    FIRE_START        = 152   # June 1
    DIABLO_START      = 258   # September 15  — offshore NE/E wind season begins
    DIABLO_PEAK       = 280   # October 7     — historically highest ignition risk
    DIABLO_END        = 319   # November 15
    FIRE_END          = 334   # November 30
    WET_ONSET         = 305   # November 1    — first significant rain probability
    FLOOD_START       = 335   # December 1
    FLOOD_END         = 90    # March 31

    lines = [f"Current date: {date_str} (day {doy} of year)"]

    # ── Fire season ────────────────────────────────────────────────────────
    if FIRE_START <= doy <= FIRE_END:
        days_in        = doy - FIRE_START + 1
        days_remaining = FIRE_END - doy
        lines.append(
            f"Fire season: ACTIVE — day {days_in} of "
            f"{FIRE_END - FIRE_START + 1} ({days_remaining} days remaining)"
        )
    elif doy < FIRE_START:
        lines.append(f"Fire season: not yet active ({FIRE_START - doy} days until June 1)")
    else:
        lines.append("Fire season: ended for this calendar year")

    # ── Diablo wind season ─────────────────────────────────────────────────
    if DIABLO_START <= doy <= DIABLO_END:
        days_in = doy - DIABLO_START + 1
        to_peak = max(0, DIABLO_PEAK - doy)
        peak_note = f", {to_peak} days to historical peak" if to_peak > 0 else " (past peak)"
        lines.append(
            f"Diablo wind season: ACTIVE — day {days_in}{peak_note}. "
            f"NE/E winds dramatically increase fire risk. Highest vigilance warranted."
        )
    elif FIRE_START <= doy < DIABLO_START:
        days_until = DIABLO_START - doy
        lines.append(
            f"Diablo wind season: {days_until} days until onset (September 15). "
            f"Begin monitoring wind direction closely as onset approaches."
        )
    else:
        lines.append("Diablo wind season: not active")

    # ── Wet season / flood risk ────────────────────────────────────────────
    if doy <= FLOOD_END:          # Jan 1 – Mar 31
        lines.append(f"Flood season: ACTIVE — {FLOOD_END - doy} days remaining (through March 31)")
    elif doy >= FLOOD_START:      # Dec 1+
        lines.append("Flood season: ACTIVE — early season, atmospheric rivers possible")
    elif WET_ONSET <= doy < FLOOD_START:   # Nov 1 – Nov 30
        lines.append(f"Flood season: {FLOOD_START - doy} days until onset. First significant rain events possible now.")
    elif DIABLO_END < doy < WET_ONSET:     # mid-Nov shoulder
        lines.append(f"Flood season: {FLOOD_START - doy} days until onset (December 1)")
    else:                          # Apr – Oct
        lines.append("Flood season: not active (dry season)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trend calculation (Phase 2)
# ---------------------------------------------------------------------------

_COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]

def _deg_to_compass(deg: float) -> str:
    return _COMPASS[round(deg / 22.5) % 16]

def _is_diablo(deg: float) -> bool:
    """NNE–ESE range (22°–112°) — offshore NE/E Diablo wind pattern."""
    return 22 <= deg <= 112


def compute_trends(grouped: dict[str, list[dict]]) -> Optional[str]:
    """Extract numeric metrics from raw domain records and compute deltas
    across the observation window.

    The agent receives prose summaries from domain nodes, but prose hides
    direction. This function makes trends explicit and pre-calculated so the
    agent doesn't have to infer "humidity is falling" from two sentences —
    it sees "-18% over 12h (falling) — fire risk building".

    Returns a formatted section string, or None if fewer than 2 data points
    exist for every domain (nothing to compute).
    """

    # (lexicon_field, display_label, unit, rise_note, fall_note)
    WATERSHED_METRICS = [
        ("dischargeMeanCfs", "River discharge (mean)", "cfs",
         "flood risk building",    "normal summer decline / drought stress if rapid"),
        ("gageHeightMaxFt",  "Gage height (max)",      "ft",
         "flood risk building",    "normal"),
    ]
    WEATHER_METRICS = [
        ("temperatureF",  "Temperature",  "°F",
         "warming",                        "cooling / fire risk easing"),
        ("humidityPct",   "Humidity",     "%",
         "fire risk easing",              "⚠ fire risk building if trend continues"),
        ("windSpeedMph",  "Wind speed",   "mph",
         "increased transport of smoke/embers", "easing"),
        ("precipMm24h",   "Precip 24h",  "mm",
         "wetting — flood risk if prolonged",   "drying"),
    ]
    AQI_METRICS = [
        ("pm25Aqi",   "PM2.5 AQI", "",
         "⚠ smoke accumulating — check wind direction", "clearing"),
        ("ozoneAqi",  "Ozone AQI", "",
         "ozone building (heat/traffic)",              "easing"),
    ]

    DOMAIN_METRICS = {
        "watershed": WATERSHED_METRICS,
        "weather":   WEATHER_METRICS,
        "aqi":       AQI_METRICS,
    }

    sections = []

    for domain, metric_defs in DOMAIN_METRICS.items():
        records = grouped.get(domain, [])
        if len(records) < 2:
            continue

        # Sort oldest → newest for delta (SQL returns newest-first)
        by_time = sorted(records, key=lambda r: r.get("observed_at", ""))
        oldest, latest = by_time[0], by_time[-1]

        oldest_raw = json.loads(oldest.get("raw_record") or "{}").get(domain, {}) or {}
        latest_raw = json.loads(latest.get("raw_record") or "{}").get(domain, {}) or {}

        try:
            t_old = datetime.fromisoformat(oldest["observed_at"].replace("Z", "+00:00"))
            t_new = datetime.fromisoformat(latest["observed_at"].replace("Z", "+00:00"))
            hours = max(0.5, (t_new - t_old).total_seconds() / 3600)
        except (KeyError, ValueError):
            hours = 12.0

        lines = []
        for field, label, unit, rise_note, fall_note in metric_defs:
            old_val = oldest_raw.get(field)
            new_val = latest_raw.get(field)
            if old_val is None or new_val is None:
                continue
            try:
                old_val, new_val = float(old_val), float(new_val)
            except (TypeError, ValueError):
                continue

            delta = new_val - old_val
            if abs(delta) < 0.05:
                direction = "stable"
                note = ""
            elif delta > 0:
                direction = "rising"
                note = rise_note
            else:
                direction = "falling"
                note = fall_note

            pct = f" ({delta / old_val * 100:+.0f}%)" if old_val != 0 else ""
            lines.append(
                f"  {label}: {old_val:.1f}{unit} → {new_val:.1f}{unit}"
                f" ({delta:+.1f}{unit}{pct} over {hours:.0f}h, {direction})"
                + (f" — {note}" if note else "")
            )

        # Wind direction: special-case because it's circular and Diablo-relevant
        old_wd = oldest_raw.get("windDirectionDeg")
        new_wd = latest_raw.get("windDirectionDeg")
        if domain == "weather" and old_wd is not None and new_wd is not None:
            old_c = _deg_to_compass(float(old_wd))
            new_c = _deg_to_compass(float(new_wd))
            diablo_note = ""
            if _is_diablo(float(new_wd)):
                diablo_note = " — ⚠ DIABLO QUADRANT (NE/E offshore flow)"
            elif _is_diablo(float(old_wd)) and not _is_diablo(float(new_wd)):
                diablo_note = " — shifting away from Diablo pattern"
            lines.append(
                f"  Wind direction: {float(old_wd):.0f}° ({old_c}) → "
                f"{float(new_wd):.0f}° ({new_c}){diablo_note}"
            )

        # Wind pattern categorical change
        old_wp = oldest_raw.get("windPattern")
        new_wp = latest_raw.get("windPattern")
        if domain == "weather" and old_wp and new_wp and old_wp != new_wp:
            diablo_flag = " ⚠" if new_wp == "diablo" else ""
            lines.append(f"  Wind pattern: {old_wp} → {new_wp}{diablo_flag}")

        if lines:
            sections.append(
                f"{domain.upper()} ({hours:.0f}h window, "
                f"{oldest['observed_at'][:16]} → {latest['observed_at'][:16]}):\n"
                + "\n".join(lines)
            )

    if not sections:
        return None

    return "=== COMPUTED TRENDS ===\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def gather_context(lookback_hours: float = 24.0,
                   memory_runs: int = 14,
                   obs_per_type: int = 6,
                   subscriber_db: Path = _DEFAULT_SUBSCRIBER_DB,
                   synthesis_db: Path = _DEFAULT_SYNTHESIS_DB) -> str:
    """Assemble the full context string passed to the synthesis agent.

    Args:
        lookback_hours:  How far back to fetch domain observations from subscriber.db.
        memory_runs:     How many prior synthesis observations to include as memory.
                         Default 14 = 7 days at twice-daily cadence.
        obs_per_type:    Max domain observations per type to include.
                         More = richer trajectory signal for the LLM.
    """
    log.info("Reading observations from subscriber DB (last %.0fh)...", lookback_hours)

    sections = []

    # ── Seasonal calendar ──────────────────────────────────────────────────
    sections.append(
        f"=== SEASONAL CALENDAR ===\n{seasonal_context()}"
    )

    # ── Memory: recent synthesis history ──────────────────────────────────
    prior = read_recent_synthesis(memory_runs, synthesis_db=synthesis_db)
    if prior:
        log.info("Memory: %d prior synthesis observations loaded", len(prior))
        sections.append(
            f"=== SYNTHESIS HISTORY (last {len(prior)} runs — oldest first) ===\n"
            f"{json.dumps(list(reversed(prior)), indent=2)}"
        )
    else:
        sections.append("=== SYNTHESIS HISTORY ===\nNone yet — first run.")

    # ── Domain observations from ATProto ───────────────────────────────────
    grouped = read_recent_observations(lookback_hours, subscriber_db=subscriber_db)

    if not grouped:
        sections.append(
            "=== NODE OBSERVATIONS ===\n"
            "No observations received from the firehose yet.\n"
            "Check that the subscriber daemon is running and nodes are publishing."
        )
    else:
        for obs_type, records in grouped.items():
            log.info("  %s: %d observation(s)", obs_type, len(records))

            publishers = list({r["node_id"] for r in records})
            section = (
                f"=== {obs_type.upper()} OBSERVATIONS "
                f"(from: {', '.join(publishers)}, "
                f"{min(len(records), obs_per_type)} of {len(records)} shown, newest first) ===\n"
            )

            for r in records[:obs_per_type]:
                raw = json.loads(r["raw_record"]) if r["raw_record"] else {}
                section += json.dumps({
                    "observed_at": r["observed_at"],
                    "node": r["node_id"],
                    "summary": r["summary"],
                    "flagged": bool(r["flagged"]),
                    # Domain-specific numeric/structured fields for trajectory reading
                    **{k: v for k, v in raw.items()
                       if k in ("watershed", "weather", "aqi") and v},
                }, indent=2) + "\n"

            sections.append(section)

        # Computed trends — pre-calculated deltas so the agent sees direction
        # explicitly rather than having to infer it from prose summaries.
        trends = compute_trends(grouped)
        if trends:
            sections.append(trends)
        else:
            log.info("Trends: insufficient data points (need ≥2 per domain)")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"Synthesis run at: {now}\n"
        f"Location: Napa Valley, California\n\n"
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


def write_observation(obs: dict, dry_run: bool = False,
                      synthesis_db: Path = _DEFAULT_SYNTHESIS_DB) -> None:
    if dry_run:
        log.info("[DRY RUN] Would write synthesis observation:")
        log.info("  Summary:      %s", obs.get("summary", ""))
        log.info("  Overall risk: %s", obs.get("overall_risk", ""))
        log.info("  Flagged:      %s", obs.get("flagged", False))
        return

    synthesis_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(synthesis_db)
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
    log.info("Synthesis observation written to %s", synthesis_db)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesis agent (ATProto version)")
    parser.add_argument("--model", choices=list(MODELS.keys()), default=DEFAULT_MODEL)
    parser.add_argument("--lookback", type=float, default=24.0,
                        help="Hours of observations to consider (default 24)")
    parser.add_argument("--memory", type=int, default=14,
                        help="Prior synthesis runs to include as memory (default 14 = 7 days)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--subscriber-db", type=Path, default=_DEFAULT_SUBSCRIBER_DB,
                        help="Path to subscriber.db (default: ../ATProto/data/subscriber.db on Pi, "
                             "override to Synthesis/data/subscriber.db when running from repo)")
    parser.add_argument("--synthesis-db", type=Path, default=_DEFAULT_SYNTHESIS_DB,
                        help="Path to synthesis output DB (default: agent/data/synthesis.db)")
    args = parser.parse_args()

    subscriber_db = args.subscriber_db
    synthesis_db  = args.synthesis_db

    log.info("=== Synthesis Agent (ATProto) starting ===")
    log.info("Model: %s  |  Dry run: %s  |  Lookback: %.0fh",
             args.model, args.dry_run, args.lookback)
    log.info("Subscriber DB: %s", subscriber_db)
    log.info("Synthesis DB:  %s", synthesis_db)

    context = gather_context(lookback_hours=args.lookback,
                             memory_runs=args.memory,
                             subscriber_db=subscriber_db,
                             synthesis_db=synthesis_db)
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Synthesis conclusion ---")
    log.info("Fire risk:    %s", observation.get("fire_risk", ""))
    log.info("Flood risk:   %s", observation.get("flood_risk", ""))
    log.info("AQI risk:     %s", observation.get("air_quality_risk", ""))
    log.info("Overall risk: %s", observation.get("overall_risk", ""))
    log.info("Flagged:      %s", observation.get("flagged", False))
    log.info("Summary: %s", observation.get("summary", ""))

    write_observation(observation, dry_run=args.dry_run, synthesis_db=synthesis_db)
    log.info("=== Synthesis Agent (ATProto) run complete ===")


if __name__ == "__main__":
    main()
