import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(os.environ.get("FIRE_DB",
                         Path.home() / "Agentic-Watershed" / "Fire" / "data" / "fire.db"))
WINDOW_H = 72          # NEAREST_HOTSPOT_MAX_AGE_HOURS (day_range 2)
FAR_MI = 50.0
RISE = 1.25            # FRP_RISE_MIN_FACTOR
PCT = 95.0             # FRP_NOTABLE_PERCENTILE
MIN_HIST = 20          # MIN_FRP_HISTORY
RADIUS_MI = 0.62       # NEW_LOCATION_RADIUS_MI
BAND = RADIUS_MI / 69.0


def hav(a, b, c, d):
    R = 3958.7613
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def notable(conn, upto):
    vals = [r[0] for r in conn.execute(
        "SELECT frp FROM hotspots WHERE frp IS NOT NULL AND collected_at <= ?"
        " ORDER BY frp ASC", (upto,))]
    if len(vals) < MIN_HIST:
        return None
    return float(vals[min(len(vals) - 1, int(len(vals) * PCT / 100))])


def gates_ok(prev, cur, thr):
    return (prev and prev > 0 and cur >= prev * RISE
            and (thr is None or cur >= thr))


def variant_a(rows, thr):
    """Current: ROUND(lat,3) cells, any rising consecutive pair, first wins."""
    keyed = sorted(rows, key=lambda r: (round(r["latitude"], 3),
                                        round(r["longitude"], 3),
                                        r["acq_date"], r["acq_time"]))
    pk = pf = None
    for r in keyed:
        k = (round(r["latitude"], 3), round(r["longitude"], 3))
        if k == pk and gates_ok(pf, r["frp"], thr):
            return True
        pk, pf = k, r["frp"]
    return False


def places(rows):
    clusters, seeds = [], []
    for r in rows:
        for i, (sla, slo) in enumerate(seeds):
            if abs(r["latitude"] - sla) <= BAND and \
                    hav(r["latitude"], r["longitude"], sla, slo) <= RADIUS_MI:
                clusters[i].append(r)
                break
        else:
            seeds.append((r["latitude"], r["longitude"]))
            clusters.append([r])
    return clusters


def variant_b(rows, thr):
    """Place grouping, any rising consecutive pair anywhere in the window."""
    for s in places(rows):
        for prev, cur in zip(s, s[1:]):
            if gates_ok(prev["frp"], cur["frp"], thr):
                return True
    return False


def variant_c(rows, thr):
    """Place grouping, latest reading against the one before it.

    Returns the strongest qualifying (previous, latest) pair, or None —
    truthy for counting, and detailed enough for --detail to say what fired.
    """
    best = None
    for s in places(rows):
        if len(s) >= 2 and gates_ok(s[-2]["frp"], s[-1]["frp"], thr):
            if best is None or s[-1]["frp"] > best[1]["frp"]:
                best = (s[-2], s[-1])
    return best


def by_pass(s):
    """One reading per satellite pass — the hottest pixel of each.

    Without this, adjacent pixels from a single overpass share an acq_time
    and read as a time series. Three of C's four firings compared two pixels
    acquired in the same minute.
    """
    out = {}
    for r in s:
        k = (r["acq_date"], r["acq_time"])
        if k not in out or (r["frp"] or 0) > (out[k]["frp"] or 0):
            out[k] = r
    return [out[k] for k in sorted(out)]


def variant_d(rows, thr):
    """Per-pass, latest pass against the one before it. (Shipped.)"""
    best = None
    for s in places(rows):
        s = by_pass(s)
        if len(s) >= 2 and gates_ok(s[-2]["frp"], s[-1]["frp"], thr):
            if best is None or s[-1]["frp"] > best[1]["frp"]:
                best = (s[-2], s[-1])
    return best


def variant_e(rows, thr):
    """Per-pass, any rising consecutive pair in the window."""
    for s in places(rows):
        s = by_pass(s)
        for prev, cur in zip(s, s[1:]):
            if gates_ok(prev["frp"], cur["frp"], thr):
                return True
    return False


DETAIL = "--detail" in sys.argv

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
runs = conn.execute(
    "SELECT observed_at, summary, flagged FROM agent_observations"
    " ORDER BY observed_at").fetchall()

n = a = b = c = d = e = 0
bc_split = []
d_fires = []
same_pass = 0
for run in runs:
    at = run["observed_at"]
    try:
        cut = (datetime.fromisoformat(at.replace("Z", "+00:00"))
               - timedelta(hours=WINDOW_H)).isoformat()
    except ValueError:
        continue
    rows = conn.execute(
        """SELECT latitude, longitude, acq_date, acq_time, frp, distance_mi
           FROM hotspots
           WHERE collected_at >= ? AND collected_at <= ? AND frp IS NOT NULL
             AND distance_mi IS NOT NULL AND distance_mi <= ?
           ORDER BY acq_date, acq_time""", (cut, at, FAR_MI)).fetchall()
    if not rows:
        continue
    thr = notable(conn, at)
    n += 1
    ra, rb = variant_a(rows, thr), variant_b(rows, thr)
    rc, rd = variant_c(rows, thr), variant_d(rows, thr)
    re_ = variant_e(rows, thr)
    a += ra
    b += rb
    c += bool(rc)
    d += bool(rd)
    e += re_
    if rb != bool(rc):
        bc_split.append((at[:16], rb, bool(rc)))
    # How much of C was two pixels from one overpass rather than a change.
    if rc and (rc[0]["acq_date"], rc[0]["acq_time"]) == \
              (rc[1]["acq_date"], rc[1]["acq_time"]):
        same_pass += 1
    if rd:
        d_fires.append((at, run["summary"], run["flagged"], rd, thr))

if not n:
    print("No runs with FRP readings in window.")
    raise SystemExit

print(f"frp_rising replayed over {n} run(s) with FRP data in window\n")
print(f"  A  round to 110m, any rising pair (current)   {a:4d}  ({100*a/n:.0f}%)")
print(f"  B  places at 1km,  any rising pair            {b:4d}  ({100*b/n:.0f}%)")
print(f"  C  places at 1km,  latest vs previous         {c:4d}  ({100*c/n:.0f}%)")
print(f"  D  per pass,       latest vs previous         {d:4d}  ({100*d/n:.0f}%)"
      "   <- shipped")
print(f"  E  per pass,       any rising pair            {e:4d}  ({100*e/n:.0f}%)")
print(f"\n  Of C's {c} firing(s), {same_pass} compared two pixels from the SAME "
      "overpass —\n  spatial variation across one burn, not a fire growing. "
      "D collapses each\n  place to its hottest pixel per pass first.")
print(f"\n  B and C differ on {len(bc_split)} run(s) — those are spikes that had")
print("  already reversed by the time the run happened.")
for at, rb, rc in bc_split[:12]:
    print(f"    {at}  B={rb} C={rc}")

if not DETAIL:
    print("\n  Re-run with --detail to see what D fires on. A plausible count "
          "is not\n  the same as real events.")
    raise SystemExit

print(f"\n=== what D fires on ({len(d_fires)} run(s)) ===")
for at, summary, flagged, (prev, cur), thr in d_fires:
    gate = f"p{PCT:g}={thr:.2f}MW" if thr is not None else "no percentile gate"
    print(f"\n  {at[:19]}   agent flagged={bool(flagged)}")
    print(f"    {prev['frp']:.2f} -> {cur['frp']:.2f} MW "
          f"(x{cur['frp'] / prev['frp']:.2f}, {gate}) "
          f"at {cur['distance_mi']:.1f}mi")
    print(f"    previous pass {prev['acq_date']} {prev['acq_time']}, "
          f"latest {cur['acq_date']} {cur['acq_time']}")
    print(f"    agent said: {(summary or '')[:220]}")
