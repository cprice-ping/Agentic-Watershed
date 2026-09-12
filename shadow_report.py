"""
Shadow verdict report — where the rules and the model disagree.

All four domains now evaluate their flag criteria twice: the model
decides `flagged`, and flag_rules.py independently evaluates the same
criteria as arithmetic into `rules_flagged` / `rules_fired`. The rules have
never been authoritative. They were added to measure the disagreement before
anyone decides whether to make them so, and this reads that measurement.

Standalone and offline, like token_report.py — nothing imports it and no
agent depends on it.

The two disagreements are not symmetric and the report keeps them apart:

  rules say flag, model did not   A missed alert if the rules are right.
                                  This is the direction that hurts, and it is
                                  what motivated the rules: the local-SLM
                                  trial (CONTEXT.md, 2026-07-16) found
                                  qwen3.5:4b reading `within 20mi OR
                                  high-confidence within 50mi` as an AND and
                                  concluding flagged=false.

  model flagged, rules did not    Either judgement the rules cannot express,
                                  or an alarm with no arithmetic behind it.
                                  Only reading the summaries tells you which.

Two caveats the report prints rather than hides.

Fire's persistence exception is deliberately absent from its rules — "a
previously-flagged low-confidence hotspot, unchanged across runs, need not be
treated as newly alarming" is a de-escalation, and a Verdict that only
accumulates has no way to record one. So Fire is expected to show
rules=1/model=0 on persistent hotspots, and that disagreement is correct
behaviour rather than a miss. Any decision to enforce has to net it out.

That was twice described here as inexpressible, which was wrong: it is a
limitation of having one output channel, not of arithmetic. Verdicts now have
two — `fire()` and `note()` — and notes appear in `rules_fired` marked with a
prefix without ever reaching `rules_flagged`. Fire's newness rule is the first
thing moved across; the de-escalation itself still is not encoded.

And a verdict is only as good as the data it read. Weather rows before the
2026-09-08 wind unit fix were computed against readings inflated 3.6x; the
migration corrected the readings but not the verdicts already recorded
against them, which cannot be recomputed. Those rows agree with the model
because both were reading the same wrong column, which is agreement that
measures nothing.

Usage:
  python3 shadow_report.py                    # last 30 days
  python3 shadow_report.py --days 90
  python3 shadow_report.py --domain fire --show-disagreements
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent

# The note channel's marker, defined once where flag_rules also reads it.
sys.path.insert(0, str(BASE))
from agent_runtime import is_note, strip_note  # noqa: E402

SOURCES = {
    "weather": BASE / "Weather" / "data" / "weather.db",
    "aqi":     BASE / "AQI"     / "data" / "aqi.db",
    "fire":    BASE / "Fire"    / "data" / "fire.db",
    "river":   BASE / "River"   / "data" / "watershed.db",
}

# River joined on 2026-09-12 and its rules test something different from the
# other three. Weather, AQI and Fire encode a documented threshold the agent
# was already told to apply, so a disagreement means one of them misread a
# criterion both were given. River has no such threshold — floodStageThresholdFt
# is unconfigured for both Napa gauges — and its one rule measures a rate of
# change nobody wrote down. So a River disagreement in the model-only direction
# is the expected case, not a defect: the agent's five low-water flags in the
# 2026-09-12 template comparison were September restated as an emergency, and
# the rules are silent on them by design. The direction worth reading for River
# is rules-only, which would be a surge the agent narrated past.
RIVER_RULES_FROM = "2026-09-12"

# Verdicts recorded before this are suspect for Weather — see module docstring.
WIND_FIX_AT = "2026-09-08"


def _rows(db: Path, since: str) -> list[sqlite3.Row]:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT observed_at, summary, flagged, rules_flagged, rules_fired,
                      model, status
               FROM agent_observations
               WHERE observed_at >= ?
               ORDER BY observed_at""",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        # A DB predating the shadow columns, or predating status.
        try:
            rows = conn.execute(
                """SELECT observed_at, summary, flagged, rules_flagged, rules_fired,
                          model
                   FROM agent_observations
                   WHERE observed_at >= ? ORDER BY observed_at""",
                (since,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    conn.close()
    return rows


def _is_failed(row) -> bool:
    try:
        return (row["status"] or "").lower() == "failed"
    except (IndexError, KeyError, TypeError):
        return False


def analyse(domain: str, rows: list[sqlite3.Row]) -> dict:
    """Split runs into agreement, each direction of disagreement, and unscored."""
    out = {"domain": domain, "both_flag": 0, "neither": 0,
           "rules_only": [], "model_only": [], "no_verdict": 0,
           "failed": 0, "fired": Counter(), "noted": Counter(),
           "first": None, "last": None}
    for r in rows:
        if _is_failed(r):
            out["failed"] += 1
            continue
        out["first"] = out["first"] or r["observed_at"]
        out["last"] = r["observed_at"]
        rules = r["rules_flagged"]
        if rules is None:
            # Either the row predates the shadow columns or the rule
            # evaluation raised. Counted, never guessed at.
            out["no_verdict"] += 1
            continue
        model = bool(r["flagged"])
        rules = bool(rules)
        try:
            for f in json.loads(r["rules_fired"] or "[]"):
                # Notes ride in the same list and must not be counted as
                # rules that fired — they never contributed to rules_flagged.
                bucket = "noted" if is_note(f) else "fired"
                out[bucket][strip_note(str(f)).split(":")[0]] += 1
        except (TypeError, ValueError):
            pass
        if model and rules:
            out["both_flag"] += 1
        elif not model and not rules:
            out["neither"] += 1
        elif rules and not model:
            out["rules_only"].append(r)
        else:
            out["model_only"].append(r)
    return out


def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:.0f}%" if total else "n/a"


def report(a: dict, show: bool) -> None:
    scored = (a["both_flag"] + a["neither"]
              + len(a["rules_only"]) + len(a["model_only"]))
    print(f"\n=== {a['domain'].upper()} ===")
    if not scored:
        print(f"  No scored runs. ({a['no_verdict']} without a verdict, "
              f"{a['failed']} failed)")
        return
    print(f"  {scored} scored run(s), {a['first'][:16]} to {a['last'][:16]}")
    if a["no_verdict"]:
        print(f"  {a['no_verdict']} run(s) carried no verdict — not counted "
              f"either way")
    if a["failed"]:
        print(f"  {a['failed']} failed run(s) excluded")

    agree = a["both_flag"] + a["neither"]
    print(f"\n  agree           {agree:4d}  ({_pct(agree, scored)})   "
          f"both flag {a['both_flag']}, neither {a['neither']}")
    print(f"  rules only      {len(a['rules_only']):4d}  "
          f"({_pct(len(a['rules_only']), scored)})   "
          f"rules said flag, model did not")
    print(f"  model only      {len(a['model_only']):4d}  "
          f"({_pct(len(a['model_only']), scored)})   "
          f"model flagged, no rule fired")

    if a["fired"]:
        print("\n  rules that fired (across all runs, including agreements):")
        for rule, n in a["fired"].most_common():
            print(f"    {n:4d}  {rule}")

    if a["noted"]:
        print("\n  notes recorded (measured, never counted toward a flag):")
        for rule, n in a["noted"].most_common():
            print(f"    {n:4d}  {rule}")

    if a["domain"] == "fire" and a["rules_only"]:
        print("\n  NOTE: Fire's persistence exception is deliberately not in "
              "\n  its rules, so some rules-only rows are the model correctly "
              "\n  declining to re-alarm on an unchanged hotspot, not a miss.")
    if a["domain"] == "river" and a["model_only"]:
        print(f"\n  NOTE: River's rules encode a rate of change, not a "
              f"threshold the agent\n  was given — no flood stage is "
              f"configured for either gauge. A model-only\n  row is the "
              f"expected shape here rather than a divergence to fix; the "
              f"\n  direction worth reading is rules-only. Rules recorded "
              f"from {RIVER_RULES_FROM}.")
    if a["domain"] == "weather":
        pre = [r for r in a["rules_only"] + a["model_only"]
               if r["observed_at"] < WIND_FIX_AT]
        if pre:
            print(f"\n  NOTE: {len(pre)} disagreement(s) predate the "
                  f"{WIND_FIX_AT} wind unit fix and were computed against "
                  f"\n  readings inflated 3.6x. Verdicts were not recomputed "
                  f"and cannot be.")

    if show:
        for label, rs in (("RULES SAID FLAG, MODEL DID NOT", a["rules_only"]),
                          ("MODEL FLAGGED, NO RULE FIRED", a["model_only"])):
            if not rs:
                continue
            print(f"\n  --- {label} ---")
            for r in rs:
                print(f"\n  {r['observed_at'][:19]}  ({r['model'] or 'unknown model'})")
                try:
                    fired = json.loads(r["rules_fired"] or "[]")
                except (TypeError, ValueError):
                    fired = []
                for f in fired:
                    print(f"    {'note' if is_note(f) else 'rule'}: "
                          f"{strip_note(f)}")
                print(f"    summary: {(r['summary'] or '')[:300]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Shadow verdict divergence report")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--domain", choices=sorted(SOURCES), default=None)
    ap.add_argument("--show-disagreements", action="store_true",
                    help="print each disagreeing run with its summary")
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    print(f"Shadow verdicts, last {args.days} day(s)")
    print("Rules are SHADOW ONLY — nothing here changed what was published.")

    targets = [args.domain] if args.domain else sorted(SOURCES)
    totals = {"rules_only": 0, "model_only": 0, "scored": 0}
    for domain in targets:
        a = analyse(domain, _rows(SOURCES[domain], since))
        report(a, args.show_disagreements)
        totals["rules_only"] += len(a["rules_only"])
        totals["model_only"] += len(a["model_only"])
        totals["scored"] += (a["both_flag"] + a["neither"]
                             + len(a["rules_only"]) + len(a["model_only"]))

    if len(targets) > 1 and totals["scored"]:
        print(f"\n=== ALL DOMAINS ===")
        print(f"  {totals['scored']} scored, "
              f"{totals['rules_only']} rules-only "
              f"({_pct(totals['rules_only'], totals['scored'])}), "
              f"{totals['model_only']} model-only "
              f"({_pct(totals['model_only'], totals['scored'])})")
        print("\n  Enforcing the rules would have changed the rules-only runs "
              "\n  from unflagged to flagged, and would have left the "
              "model-only \n  runs flagged only if the model keeps its say.")


if __name__ == "__main__":
    main()
