"""
Token / cost report — where the monthly API bill actually goes.

Reads the token counts recorded with each agent observation and prices them
per model, broken down by domain. Standalone offline tool, like
extract_training_data.py; nothing imports it and no agent depends on it.

The point is to replace an estimate with a measurement. The last time spend
was investigated here (CONTEXT.md, 2026-07-15) the answer was counter-intuitive
— Haiku on the domain agents was costing 4-5x Sonnet on Synthesis, which
per-token pricing does not predict, and the real cause was prompt size from a
memory-readback bug. Per-token price is not the thing to optimise blind.

Rows written before token logging existed have NULL counts and are reported
separately rather than counted as zero.

Usage:
  python3 token_report.py                 # last 30 days
  python3 token_report.py --days 7
  python3 token_report.py --days 90 --batch   # show Batch API pricing (50% off)
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent

# (db path, table) per domain. Synthesis keeps its own table shape.
SOURCES = {
    "river":     (BASE / "River"   / "data" / "watershed.db", "agent_observations"),
    "weather":   (BASE / "Weather" / "data" / "weather.db",   "agent_observations"),
    "aqi":       (BASE / "AQI"     / "data" / "aqi.db",       "agent_observations"),
    "fire":      (BASE / "Fire"    / "data" / "fire.db",      "agent_observations"),
    "synthesis": (BASE / "Synthesis" / "data" / "synthesis.db", "synthesis_observations"),
}

# USD per million tokens, first-party Anthropic API rates.
PRICING = {
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-sonnet-5":   (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-opus-5":     (5.00, 25.00),
}

BATCH_DISCOUNT = 0.5


def _price(model: str, in_tok: int, out_tok: int) -> float | None:
    """Cost in USD, or None when the model id has no pricing entry — an
    unpriced model should show as unknown rather than silently as $0.00."""
    rates = PRICING.get((model or "").strip())
    if rates is None:
        return None
    return in_tok / 1e6 * rates[0] + out_tok / 1e6 * rates[1]


def collect(days: float) -> tuple[dict, int, list[str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    by_key: dict[tuple[str, str], dict] = {}
    missing = 0
    notes: list[str] = []

    for domain, (db_path, table) in SOURCES.items():
        if not db_path.exists():
            notes.append(f"{domain}: no database at {db_path}")
            continue
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not {"input_tokens", "output_tokens", "model"} <= cols:
            notes.append(f"{domain}: no token columns yet — run an agent once after deploying")
            conn.close()
            continue

        rows = conn.execute(
            f"""SELECT model, input_tokens, output_tokens FROM {table}
                WHERE observed_at >= ?""", (cutoff,)
        ).fetchall()
        conn.close()

        for model, tin, tout in rows:
            if tin is None and tout is None:
                missing += 1
                continue
            key = (domain, model or "unknown")
            acc = by_key.setdefault(key, {"runs": 0, "in": 0, "out": 0})
            acc["runs"] += 1
            acc["in"] += tin or 0
            acc["out"] += tout or 0

    return by_key, missing, notes


def main() -> None:
    ap = argparse.ArgumentParser(description="Token / cost report")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--batch", action="store_true",
                    help="Also show the same spend at Batch API rates (50%% off)")
    args = ap.parse_args()

    by_key, missing, notes = collect(args.days)

    print(f"\nToken usage — last {args.days:g} days\n")
    print(f"{'domain':<11} {'model':<20} {'runs':>5} {'in':>10} {'out':>8} {'cost':>9}")
    print("-" * 68)

    total = 0.0
    unpriced: set[str] = set()
    for (domain, model), acc in sorted(by_key.items()):
        cost = _price(model, acc["in"], acc["out"])
        if cost is None:
            unpriced.add(model)
            shown = "unpriced"
        else:
            total += cost
            shown = f"${cost:.2f}"
        print(f"{domain:<11} {model:<20} {acc['runs']:>5} "
              f"{acc['in']:>10,} {acc['out']:>8,} {shown:>9}")

    if not by_key:
        print("  (no rows with token counts yet)")

    print("-" * 68)
    print(f"{'total':<38} {'':>10} {'':>8} ${total:>8.2f}")

    if args.days:
        monthly = total / args.days * 30.0
        print(f"{'projected 30-day':<38} {'':>10} {'':>8} ${monthly:>8.2f}")
        if args.batch:
            print(f"{'  same spend at Batch API rates':<38} {'':>10} {'':>8} "
                  f"${monthly * BATCH_DISCOUNT:>8.2f}")

    if missing:
        print(f"\n{missing} observation(s) had no token counts — written before "
              f"token logging existed. Not counted as zero.")
    for m in sorted(unpriced):
        print(f"\nNo pricing entry for {m!r} — add it to PRICING to include it.")
    for n in notes:
        print(f"\n{n}")
    print()


if __name__ == "__main__":
    main()
