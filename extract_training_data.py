"""
Training data extraction — reconstructs (context, response) pairs from
existing history for LoRA fine-tuning experiments. Not wired into any
agent; a standalone offline tool.

Why this works without new logging: every domain agent's gather_context()
is just a set of deterministic SQL queries against its own collector DB,
filtered by time. Since those tables are append-only and every row carries
its own collected_at, the exact context a given historical agent run would
have seen can be replayed by re-running the same query logic with an
"as of observed_at" cutoff instead of "as of now" — paired with that run's
actual stored response (agent_observations.summary/flagged/reasoning),
that's a real distillation dataset with no new instrumentation required.

Important: every query below adds an explicit `collected_at <= :asof` (or
`polled_at <= :asof`) constraint even where the live MCP tool doesn't have
one, because the live tool always implicitly runs "as of now" — when
replaying a past `asof`, that constraint has to be made explicit or a
"latest reading" query would leak future data into a reconstructed past
context.

Usage:
  python3 extract_training_data.py --domain fire
  python3 extract_training_data.py --domain weather
  python3 extract_training_data.py --domain aqi
  python3 extract_training_data.py --domain river
  python3 extract_training_data.py --domain all

Output: <Domain>/data/training_examples.jsonl (gitignored — this is derived
data, not code). One JSON object per line:
  {"domain": ..., "observed_at": ..., "context": ..., "response": {...}}
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
NODE_CFG = json.loads((BASE / "node_config.json").read_text())


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _shift(asof: str, **delta) -> str:
    return (datetime.fromisoformat(asof) - timedelta(**delta)).isoformat()


def _memory(conn: sqlite3.Connection, asof: str, n: int = 3) -> str:
    """Same shape as the fixed get_recent_observations/get_recent_agent_observations —
    summary/flagged only, and only rows strictly before this one (no future leakage)."""
    rows = conn.execute(
        """
        SELECT observed_at, summary, flagged
        FROM agent_observations
        WHERE observed_at < ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        (asof, n),
    ).fetchall()
    if not rows:
        return "No previous observations recorded. This appears to be a fresh run."
    return json.dumps(_rows(rows), indent=2)


# ---------------------------------------------------------------------------
# Fire
# ---------------------------------------------------------------------------

def fire_context(conn: sqlite3.Connection, asof: str) -> str:
    sections = ["=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n" + _memory(conn, asof)]

    last_poll = conn.execute(
        "SELECT polled_at, status, error_message FROM polls WHERE polled_at <= ? ORDER BY polled_at DESC LIMIT 1",
        (asof,),
    ).fetchone()
    poll_status = json.dumps(dict(last_poll), indent=2) if last_poll else json.dumps({"status": "never_polled"})
    sections.append("=== COLLECTOR STATUS (is the poller itself healthy?) ===\n" + poll_status)

    currency_cutoff = _shift(asof, hours=72)
    nearest = conn.execute(
        """
        SELECT latitude, longitude, acq_date, acq_time, satellite, confidence, frp, daynight, distance_mi
        FROM hotspots
        WHERE collected_at >= ? AND collected_at <= ?
        ORDER BY distance_mi ASC LIMIT 10
        """,
        (currency_cutoff, asof),
    ).fetchall()
    nearest_result = {
        "last_poll_at": last_poll["polled_at"] if last_poll else None,
        "last_poll_status": last_poll["status"] if last_poll else "never_polled",
        "currency_window_hours": 72,
        "nearest_hotspots": _rows(nearest),
    }
    sections.append("=== NEAREST HOTSPOTS ===\n" + json.dumps(nearest_result, indent=2))

    cutoff_24h = _shift(asof, hours=24)
    count_row = conn.execute(
        """
        SELECT COUNT(*) as n, MIN(distance_mi) as closest_mi, MAX(frp) as max_frp
        FROM hotspots WHERE collected_at >= ? AND collected_at <= ? AND distance_mi <= 50
        """,
        (cutoff_24h, asof),
    ).fetchone()
    sections.append("=== HOTSPOT COUNT: LAST 24H WITHIN 50MI ===\n" + json.dumps(dict(count_row), indent=2))

    cutoff_48h = _shift(asof, hours=48)
    since = conn.execute(
        """
        SELECT latitude, longitude, acq_date, acq_time, satellite, confidence, frp, daynight, distance_mi
        FROM hotspots WHERE collected_at >= ? AND collected_at <= ? ORDER BY distance_mi ASC
        """,
        (cutoff_48h, asof),
    ).fetchall()
    sections.append("=== HOTSPOTS: LAST 48 HOURS (nearest first) ===\n" + json.dumps(_rows(since), indent=2))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

def weather_context(conn: sqlite3.Connection, asof: str) -> str:
    sections = ["=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n" + _memory(conn, asof)]

    latest = conn.execute(
        "SELECT * FROM observations WHERE collected_at <= ? ORDER BY collected_at DESC LIMIT 1", (asof,)
    ).fetchone()
    cutoff_7d = _shift(asof, days=7)
    stats = conn.execute(
        """
        SELECT COUNT(*) as n_readings, MIN(temperature_f) as min_temp_f, AVG(temperature_f) as avg_temp_f,
               MAX(temperature_f) as max_temp_f, MIN(humidity_pct) as min_humidity, AVG(humidity_pct) as avg_humidity,
               MAX(wind_speed_mph) as max_wind_mph, MAX(wind_gust_mph) as max_gust_mph,
               SUM(precip_1h_mm) as total_precip_mm
        FROM observations WHERE collected_at >= ? AND collected_at <= ?
        """,
        (cutoff_7d, asof),
    ).fetchone()
    current_result = {"current": dict(latest) if latest else {}, "seven_day_stats": dict(stats) if stats else {}}
    sections.append("=== CURRENT CONDITIONS ===\n" + json.dumps(current_result, indent=2))

    alerts = conn.execute(
        """
        SELECT event, severity, urgency, headline, onset, expires, zones
        FROM alerts
        WHERE collected_at <= ? AND (expires IS NULL OR expires > ?)
        GROUP BY alert_id ORDER BY collected_at DESC
        """,
        (asof, asof),
    ).fetchall()
    if not alerts:
        alerts_text = "No active NWS alerts for Napa County."
    else:
        alerts_text = json.dumps(_rows(alerts), indent=2)
    sections.append("=== ACTIVE NWS ALERTS ===\n" + alerts_text)

    cutoff_48h = _shift(asof, hours=48)
    trend = conn.execute(
        """
        SELECT MIN(humidity_pct) as min_humidity_48h, AVG(humidity_pct) as avg_humidity_48h,
               MAX(temperature_f) as max_temp_48h, MAX(wind_speed_mph) as max_wind_48h,
               MAX(wind_gust_mph) as max_gust_48h, SUM(COALESCE(precip_1h_mm, 0)) as total_precip_48h_mm
        FROM observations WHERE collected_at >= ? AND collected_at <= ?
        """,
        (cutoff_48h, asof),
    ).fetchone()
    cutoff_7d_rain = _shift(asof, days=7)
    last_rain = conn.execute(
        """
        SELECT collected_at, precip_1h_mm FROM observations
        WHERE precip_1h_mm > 1.0 AND collected_at >= ? AND collected_at <= ?
        ORDER BY collected_at DESC LIMIT 1
        """,
        (cutoff_7d_rain, asof),
    ).fetchone()
    fire_result = {
        "current": dict(latest) if latest else {},
        "trend_48h": dict(trend) if trend else {},
        "last_significant_rain": dict(last_rain) if last_rain else "None in last 7 days",
    }
    sections.append("=== FIRE RISK INDICATORS ===\n" + json.dumps(fire_result, indent=2))

    since = conn.execute(
        """
        SELECT collected_at, obs_time, temperature_f, humidity_pct, wind_speed_mph, wind_direction_deg,
               wind_gust_mph, precip_1h_mm, precip_6h_mm, precip_24h_mm, text_description
        FROM observations WHERE collected_at >= ? AND collected_at <= ? ORDER BY collected_at ASC
        """,
        (cutoff_48h, asof),
    ).fetchall()
    sections.append("=== OBSERVATIONS: LAST 48 HOURS ===\n" + json.dumps(_rows(since), indent=2))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# AQI
# ---------------------------------------------------------------------------

def aqi_context(conn: sqlite3.Connection, asof: str) -> str:
    sections = ["=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n" + _memory(conn, asof)]

    current = conn.execute(
        """
        SELECT parameter, aqi, category_number, category_name, obs_date, obs_hour, reporting_area, collected_at
        FROM observations
        WHERE collected_at <= ?
        GROUP BY parameter HAVING collected_at = MAX(collected_at)
        ORDER BY parameter
        """,
        (asof,),
    ).fetchall()
    sections.append("=== CURRENT AQI (PM2.5 and OZONE) ===\n" + json.dumps(_rows(current), indent=2))

    cutoff_3h = _shift(asof, hours=3)
    cutoff_24h = _shift(asof, hours=24)
    cutoff_7d = _shift(asof, days=7)
    pm25_now = conn.execute(
        """
        SELECT aqi, category_number, category_name, collected_at, obs_hour FROM observations
        WHERE parameter = 'PM2.5' AND collected_at <= ? ORDER BY collected_at DESC LIMIT 1
        """,
        (asof,),
    ).fetchone()
    pm25_3h_ago = conn.execute(
        """
        SELECT aqi, collected_at FROM observations
        WHERE parameter = 'PM2.5' AND collected_at <= ? ORDER BY collected_at DESC LIMIT 1
        """,
        (cutoff_3h,),
    ).fetchone()
    peak_24h = conn.execute(
        """
        SELECT MAX(aqi) as peak_aqi, MAX(category_number) as worst_cat FROM observations
        WHERE parameter = 'PM2.5' AND collected_at >= ? AND collected_at <= ?
        """,
        (cutoff_24h, asof),
    ).fetchone()
    peak_7d = conn.execute(
        """
        SELECT MAX(aqi) as peak_aqi, DATE(collected_at) as peak_date FROM observations
        WHERE parameter = 'PM2.5' AND collected_at >= ? AND collected_at <= ?
        """,
        (cutoff_7d, asof),
    ).fetchone()
    unhealthy = conn.execute(
        """
        SELECT COUNT(*) as n, MAX(aqi) as worst_aqi FROM observations
        WHERE parameter = 'PM2.5' AND aqi > 150 AND collected_at >= ? AND collected_at <= ?
        """,
        (cutoff_7d, asof),
    ).fetchone()
    ozone = conn.execute(
        """
        SELECT aqi, category_name FROM observations
        WHERE parameter = 'OZONE' AND collected_at <= ? ORDER BY collected_at DESC LIMIT 1
        """,
        (asof,),
    ).fetchone()
    aqi_change = None
    rising_rapidly = False
    if pm25_now and pm25_3h_ago and pm25_now["aqi"] and pm25_3h_ago["aqi"]:
        aqi_change = pm25_now["aqi"] - pm25_3h_ago["aqi"]
        rising_rapidly = aqi_change >= 20
    smoke_result = {
        "pm25_current": dict(pm25_now) if pm25_now else None,
        "pm25_change_3h": aqi_change,
        "pm25_rising_rapidly": rising_rapidly,
        "pm25_peak_24h": dict(peak_24h) if peak_24h else None,
        "pm25_peak_7d": dict(peak_7d) if peak_7d else None,
        "pm25_unhealthy_readings_7d": dict(unhealthy) if unhealthy else None,
        "ozone_current": dict(ozone) if ozone else None,
    }
    sections.append("=== SMOKE INDICATORS ===\n" + json.dumps(smoke_result, indent=2))

    cutoff_48h = _shift(asof, hours=48)
    since = conn.execute(
        """
        SELECT collected_at, parameter, aqi, category_name, obs_date, obs_hour FROM observations
        WHERE collected_at >= ? AND collected_at <= ? ORDER BY parameter, collected_at ASC
        """,
        (cutoff_48h, asof),
    ).fetchall()
    sections.append("=== AQI READINGS: LAST 48 HOURS ===\n" + json.dumps(_rows(since), indent=2))

    trend = conn.execute(
        """
        SELECT DATE(collected_at) as date, parameter, MIN(aqi) as min_aqi, ROUND(AVG(aqi), 1) as avg_aqi,
               MAX(aqi) as max_aqi, MAX(category_number) as worst_category, COUNT(*) as n_readings
        FROM observations WHERE collected_at >= ? AND collected_at <= ? AND aqi IS NOT NULL
        GROUP BY DATE(collected_at), parameter ORDER BY date ASC, parameter
        """,
        (cutoff_7d, asof),
    ).fetchall()
    sections.append("=== DAILY AQI TREND: LAST 7 DAYS ===\n" + json.dumps(_rows(trend), indent=2))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# River
# ---------------------------------------------------------------------------

def river_context(conn: sqlite3.Connection, asof: str) -> str:
    sections = ["=== PREVIOUS AGENT OBSERVATIONS (memory) ===\n" + _memory(conn, asof)]

    cutoff_7d = _shift(asof, days=7)
    for station_id, station_name in NODE_CFG["watershed"]["usgs_stations"].items():
        latest = conn.execute(
            """
            SELECT parameter_name, value, unit, qualifier, collected_at, usgs_datetime
            FROM readings WHERE station_id = ? AND collected_at <= ?
            GROUP BY parameter_code HAVING collected_at = MAX(collected_at)
            """,
            (station_id, asof),
        ).fetchall()
        stats = conn.execute(
            """
            SELECT parameter_name, unit, COUNT(*) as n_readings, MIN(value) as min_val,
                   AVG(value) as mean_val, MAX(value) as max_val
            FROM readings WHERE station_id = ? AND collected_at >= ? AND collected_at <= ? AND value IS NOT NULL
            GROUP BY parameter_code
            """,
            (station_id, cutoff_7d, asof),
        ).fetchall()
        result = {"station_id": station_id, "latest": _rows(latest), "seven_day_stats": _rows(stats)}
        sections.append(f"=== STATION: {station_name.upper()} ({station_id}) ===\n" + json.dumps(result, indent=2))

    cutoff_30d = _shift(asof, days=30)
    cutoff_24h = _shift(asof, hours=24)
    baselines = conn.execute(
        """
        SELECT station_id, parameter_code, parameter_name, AVG(value) as mean_val, unit
        FROM readings WHERE collected_at >= ? AND collected_at <= ? AND value IS NOT NULL
        GROUP BY station_id, parameter_code
        """,
        (cutoff_30d, asof),
    ).fetchall()
    recent = conn.execute(
        """
        SELECT station_id, parameter_code, parameter_name, value, unit, collected_at, qualifier
        FROM readings WHERE collected_at >= ? AND collected_at <= ? AND value IS NOT NULL
        ORDER BY collected_at DESC
        """,
        (cutoff_24h, asof),
    ).fetchall()
    baseline_map = {(r["station_id"], r["parameter_code"]): r["mean_val"] for r in baselines}
    anomalies = []
    for row in recent:
        key = (row["station_id"], row["parameter_code"])
        mean = baseline_map.get(key)
        if mean is None or mean == 0:
            continue
        deviation_pct = abs(row["value"] - mean) / abs(mean) * 100
        if deviation_pct >= 40.0:
            d = dict(row)
            d["baseline_mean"] = round(mean, 3)
            d["deviation_pct"] = round(deviation_pct, 1)
            anomalies.append(d)
    anomalies.sort(key=lambda x: x["deviation_pct"], reverse=True)
    anomalies_text = json.dumps(anomalies, indent=2) if anomalies else "No anomalies detected (>40% deviation) in the last 24 hours."
    sections.append("=== ANOMALY SCAN (>40% deviation from 30-day mean) ===\n" + anomalies_text)

    cutoff_48h = _shift(asof, hours=48)
    since = conn.execute(
        """
        SELECT collected_at, station_id, station_name, parameter_name, value, unit, qualifier
        FROM readings WHERE collected_at >= ? AND collected_at <= ?
        ORDER BY station_id, parameter_code, collected_at
        """,
        (cutoff_48h, asof),
    ).fetchall()
    sections.append("=== READINGS: LAST 48 HOURS ===\n" + json.dumps(_rows(since), indent=2))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Extraction driver
# ---------------------------------------------------------------------------

DOMAINS = {
    "fire":    {"db": BASE / "Fire" / "data" / "fire.db", "context_fn": fire_context},
    "weather": {"db": BASE / "Weather" / "data" / "weather.db", "context_fn": weather_context},
    "aqi":     {"db": BASE / "AQI" / "data" / "aqi.db", "context_fn": aqi_context},
    "river":   {"db": BASE / "River" / "data" / "watershed.db", "context_fn": river_context},
}


def extract_domain(domain: str) -> int:
    cfg = DOMAINS[domain]
    db_path = cfg["db"]
    if not db_path.exists():
        print(f"[{domain}] DB not found at {db_path}, skipping")
        return 0

    conn = _db(db_path)
    rows = conn.execute(
        "SELECT observed_at, summary, flagged, reasoning FROM agent_observations ORDER BY observed_at ASC"
    ).fetchall()

    out_path = db_path.parent / "training_examples.jsonl"
    n_written = 0
    with open(out_path, "w") as f:
        for row in rows:
            asof = row["observed_at"]
            try:
                context = cfg["context_fn"](conn, asof)
            except sqlite3.Error as exc:
                print(f"[{domain}] Failed to reconstruct context for {asof}: {exc}")
                continue
            example = {
                "domain": domain,
                "observed_at": asof,
                "context": context,
                "response": {
                    "summary": row["summary"],
                    "flagged": bool(row["flagged"]),
                    "reasoning": row["reasoning"],
                },
            }
            f.write(json.dumps(example) + "\n")
            n_written += 1

    conn.close()
    print(f"[{domain}] {n_written} examples written to {out_path}")
    return n_written


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract (context, response) training pairs from agent history")
    parser.add_argument("--domain", choices=[*DOMAINS.keys(), "all"], default="all")
    args = parser.parse_args()

    domains = list(DOMAINS.keys()) if args.domain == "all" else [args.domain]
    total = sum(extract_domain(d) for d in domains)
    print(f"\nTotal: {total} examples across {len(domains)} domain(s)")


if __name__ == "__main__":
    main()
