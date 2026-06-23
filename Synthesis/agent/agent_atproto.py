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

# ---------------------------------------------------------------------------
# Prediction resolution constants (Phase 3)
# ---------------------------------------------------------------------------
# Encoding thresholds as constants prevents drift — the agent can't argue itself
# into a looser definition of "confirmed" over successive runs.

FLOOD_ACTION_STAGE_FT = 12.0   # Gage height at which flooding begins to affect low-lying areas
AQI_USG_THRESHOLD     = 100    # EPA PM2.5 "Unhealthy for Sensitive Groups" threshold
FIRE_CONFIRM_LEVELS   = frozenset({"high", "extreme"})  # weather.fireRisk values that confirm
FIRE_CONFIRM_ALERTS   = frozenset({"Red Flag Warning", "Fire Weather Watch"})  # NWS alert names

# How long a prediction stays pending before auto-expiry if no confirming observation arrives.
# After the window closes the event either happened or didn't — leaving it pending pollutes calibration.
PREDICTION_HORIZON_HOURS: dict[str, int] = {
    "fire":        48,   # Fire weather develops fast; 2-day window is sufficient
    "flood":       72,   # Atmospheric rivers have multi-day lead times
    "air_quality": 24,   # Smoke disperses or accumulates quickly
}

# Only write prediction records for these risk levels — none/low generate too many
# false positives to be calibration-useful.
PREDICTION_RISK_LEVELS = frozenset({"moderate", "high", "extreme"})

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

COMPUTED TRENDS may not be present if this is the first run or data was unavailable in the
lookback window. If the trends section is absent or shows insufficient data, rely on prose
summaries and synthesis history alone for trajectory assessment.

PREDICTION LEDGER: You will see a summary of how your past risk assessments have resolved.
Use the confirmed vs expired ratio to calibrate confidence — many false positives means you
are being too aggressive; raise your threshold. You do not need to phrase predictions in your
summary. A prediction record is written automatically for every domain you assess as moderate+
risk. Focus on honest risk assessment; outcome tracking is handled externally.

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
    """Return True if wind direction is in the Diablo quadrant (NNE–ESE, 22°–112°).

    Meteorological rationale: Diablo winds are offshore, downslope flows from the
    interior Coast Ranges toward the Bay. They arrive from the NE–E quadrant,
    compressing and warming adiabatically as they descend. The 22°–112° range covers
    NNE through ESE — the full sector associated with offshore flow in the Napa/Sonoma
    region. Winds outside this range (N, S, W) are typically onshore marine or valley
    breezes and do not carry the same fire risk multiplier.
    """
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
# Prediction ledger (Phase 3)
# ---------------------------------------------------------------------------

def _ensure_predictions_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            made_at         TEXT NOT NULL,
            risk_type       TEXT NOT NULL,       -- 'fire' | 'flood' | 'air_quality'
            predicted_level TEXT NOT NULL,       -- 'moderate' | 'high' | 'extreme'
            horizon_hours   INTEGER NOT NULL,    -- resolution window in hours
            status          TEXT NOT NULL DEFAULT 'pending',
                                                 -- 'pending'|'confirmed'|'false_positive'|'expired'
            resolved_at     TEXT,
            resolution_note TEXT
        )
    """)
    conn.commit()


def _resolve_prediction(risk_type: str, grouped: dict[str, list[dict]]) -> Optional[str]:
    """Check whether current observations satisfy the resolution criteria for a prediction.

    Returns a short note string if the prediction is confirmed, None if not yet confirmed.
    Thresholds are the module-level constants — not inferred from context.
    """
    if risk_type == "fire":
        for rec in grouped.get("weather", []):
            raw = json.loads(rec.get("raw_record") or "{}").get("weather", {}) or {}
            if raw.get("fireRisk") in FIRE_CONFIRM_LEVELS:
                return f"weather.fireRisk={raw['fireRisk']}"
            hit = FIRE_CONFIRM_ALERTS & set(raw.get("activeAlerts") or [])
            if hit:
                return f"NWS alert: {', '.join(sorted(hit))}"

    elif risk_type == "flood":
        for rec in grouped.get("watershed", []):
            raw = json.loads(rec.get("raw_record") or "{}").get("watershed", {}) or {}
            gage = raw.get("gageHeightMaxFt")
            if gage is not None and float(gage) >= FLOOD_ACTION_STAGE_FT:
                return (f"gageHeightMaxFt={float(gage):.1f}ft "
                        f"(\u2265 action stage {FLOOD_ACTION_STAGE_FT}ft)")

    elif risk_type == "air_quality":
        for rec in grouped.get("aqi", []):
            raw = json.loads(rec.get("raw_record") or "{}").get("aqi", {}) or {}
            pm25 = raw.get("pm25Aqi")
            if pm25 is not None and int(pm25) >= AQI_USG_THRESHOLD:
                return f"pm25Aqi={pm25} (\u2265 USG threshold {AQI_USG_THRESHOLD})"
            if raw.get("smokeDetected"):
                return "smokeDetected=true"

    return None


def check_predictions(grouped: dict[str, list[dict]],
                      synthesis_db: Path,
                      dry_run: bool = False) -> Optional[str]:
    """Resolve open predictions against current observations; return a prompt-ready summary.

    Called at the TOP of gather_context() so the agent always sees an up-to-date ledger.
    Expired predictions are marked automatically — a prediction that sits unresolved past
    its horizon_hours is 'expired', not left pending to pollute calibration data.
    """
    if not synthesis_db.exists():
        return None

    try:
        conn = sqlite3.connect(synthesis_db)
        conn.row_factory = sqlite3.Row
        _ensure_predictions_table(conn)

        now     = datetime.now(timezone.utc)
        now_str = now.isoformat()

        for row in [dict(r) for r in conn.execute(
            "SELECT * FROM predictions WHERE status = 'pending'"
        ).fetchall()]:
            made_at = datetime.fromisoformat(row["made_at"].replace("Z", "+00:00"))
            note    = _resolve_prediction(row["risk_type"], grouped)

            if note:
                new_status = "confirmed"
            elif (now - made_at) > timedelta(hours=row["horizon_hours"]):
                new_status = "expired"
                note = f"No confirming observations within {row['horizon_hours']}h window"
            else:
                continue  # Still within window — leave pending

            if not dry_run:
                conn.execute(
                    """UPDATE predictions
                       SET status = ?, resolved_at = ?, resolution_note = ?
                       WHERE id = ?""",
                    (new_status, now_str, note, row["id"]),
                )
            log.info("Prediction %d (%s): pending \u2192 %s | %s",
                     row["id"], row["risk_type"], new_status, note)

        if not dry_run:
            conn.commit()

        # Build compact prompt summary from last 30 days
        cutoff_30d = (now - timedelta(days=30)).isoformat()
        rows = conn.execute(
            """SELECT risk_type, predicted_level, status, made_at, resolution_note
               FROM predictions WHERE made_at >= ?
               ORDER BY made_at DESC""",
            (cutoff_30d,),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        count_str = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))

        lines = [
            f"=== PREDICTION LEDGER (last 30 days \u2014 {len(rows)} total: {count_str}) ===",
            "Recent predictions (newest first):",
        ]
        for p in [dict(r) for r in rows[:5]]:
            age_h = (now - datetime.fromisoformat(
                p["made_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            line  = (f"  [{p['status'].upper()}] {p['risk_type']} \u2192 {p['predicted_level']} "
                     f"({age_h:.0f}h ago)")
            if p["resolution_note"]:
                line += f": {p['resolution_note']}"
            lines.append(line)

        return "\n".join(lines)

    except sqlite3.Error as exc:
        log.error("Failed to check predictions: %s", exc)
        return None


def write_predictions(obs: dict,
                      synthesis_db: Path,
                      dry_run: bool = False) -> None:
    """Write prediction records for any domain assessed as moderate+ risk.

    Called BEFORE write_observation() so the ledger is updated even if the
    container is interrupted between the agent and publisher steps.
    """
    to_predict = [
        (domain, level)
        for domain, level in (
            ("fire",        obs.get("fire_risk", "none")),
            ("flood",       obs.get("flood_risk", "none")),
            ("air_quality", obs.get("air_quality_risk", "none")),
        )
        if level in PREDICTION_RISK_LEVELS
    ]

    if not to_predict:
        log.info("Predictions: no domains at moderate+ risk \u2014 no new predictions written")
        return

    if dry_run:
        for domain, level in to_predict:
            log.info("[DRY RUN] Would write prediction: %s risk = %s (horizon %dh)",
                     domain, level, PREDICTION_HORIZON_HOURS.get(domain, 48))
        return

    synthesis_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(synthesis_db)
    _ensure_predictions_table(conn)
    now = datetime.now(timezone.utc).isoformat()

    for domain, level in to_predict:
        horizon = PREDICTION_HORIZON_HOURS.get(domain, 48)
        conn.execute(
            """INSERT INTO predictions (made_at, risk_type, predicted_level, horizon_hours)
               VALUES (?, ?, ?, ?)""",
            (now, domain, level, horizon),
        )
        log.info("Prediction written: %s risk = %s (expires in %dh)", domain, level, horizon)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def gather_context(lookback_hours: float = 24.0,
                   memory_runs: int = 14,
                   obs_per_type: int = 6,
                   subscriber_db: Path = _DEFAULT_SUBSCRIBER_DB,
                   synthesis_db: Path = _DEFAULT_SYNTHESIS_DB,
                   dry_run: bool = False) -> str:
    """Assemble the full context string passed to the synthesis agent.

    Args:
        lookback_hours:  How far back to fetch domain observations from subscriber.db.
        memory_runs:     How many prior synthesis observations to include as memory.
                         Default 14 = 7 days at twice-daily cadence.
        obs_per_type:    Max domain observations per type to include.
        dry_run:         If True, prediction resolution is logged but not written to DB.
    """
    log.info("Reading observations from subscriber DB (last %.0fh)...", lookback_hours)

    sections = []

    # ── Fetch observations first — needed for prediction resolution ────────
    grouped = read_recent_observations(lookback_hours, subscriber_db=subscriber_db)

    # ── Resolve open predictions against current observations ──────────────
    # Must happen before building the prompt so the agent sees the updated ledger.
    pred_summary = check_predictions(grouped, synthesis_db, dry_run=dry_run)

    # ── Seasonal calendar ──────────────────────────────────────────────────
    sections.append(
        f"=== SEASONAL CALENDAR ===\n{seasonal_context()}"
    )

    # ── Prediction ledger ──────────────────────────────────────────────────
    if pred_summary:
        sections.append(pred_summary)
    else:
        sections.append("=== PREDICTION LEDGER ===\nNo prediction history yet.")

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
            log.info("Trends: insufficient data (need \u22652 observations per domain)")
            sections.append(
                "=== COMPUTED TRENDS ===\n"
                "Insufficient data for trend calculation (need \u22652 observations per domain). "
                "Assess trajectory from prose summaries and synthesis history."
            )

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
                             synthesis_db=synthesis_db,
                             dry_run=args.dry_run)
    observation = reason(context, args.model, verbose=args.verbose)

    log.info("--- Synthesis conclusion ---")
    log.info("Fire risk:    %s", observation.get("fire_risk", ""))
    log.info("Flood risk:   %s", observation.get("flood_risk", ""))
    log.info("AQI risk:     %s", observation.get("air_quality_risk", ""))
    log.info("Overall risk: %s", observation.get("overall_risk", ""))
    log.info("Flagged:      %s", observation.get("flagged", False))
    log.info("Summary: %s", observation.get("summary", ""))

    # Write predictions BEFORE the synthesis observation — the ledger is updated
    # even if the container is interrupted before publisher.py runs.
    write_predictions(observation, synthesis_db=synthesis_db, dry_run=args.dry_run)
    write_observation(observation, dry_run=args.dry_run, synthesis_db=synthesis_db)
    log.info("=== Synthesis Agent (ATProto) run complete ===")


if __name__ == "__main__":
    main()
