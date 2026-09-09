"""
Deterministic flag rules — Fire
-------------------------------
The flag criteria in agent.py's system prompt are arithmetic over columns the
collector already stores: distance_mi, confidence, frp, and the polls table.
This module evaluates them in code so the decision does not depend on a model
reading a rule correctly.

That is not hypothetical. The local-SLM trial (CONTEXT.md, 2026-07-16) found
qwen3.5:4b reading `within 20mi OR high-confidence within 50mi` as an AND and
concluding flagged=false — it "would have suppressed a real alert." A rule of
this shape should not be a language problem.

Currently SHADOW ONLY. The verdict is recorded next to the model's own
flagged value; it does not override it. Enforcing it is a separate decision
that should be made against the divergence data this produces, not in advance
— see the persistence-exception note below for why that matters here.

Windows come from thresholds.py, the same module mcp_server.py reads, so a
divergence means the model and the rules disagreed about the same facts rather
than about which rows were in scope.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

# Every threshold comes from thresholds.py, which agent.py's prompt,
# mcp_server.py's queries and ATProto/publisher.py's currency window are also
# read from. The currency window in particular used to exist in three copies
# and be absent from a fourth place that needed it.
from thresholds import (  # noqa: E402
    NEAREST_HOTSPOT_MAX_AGE_HOURS, NEAR_DISTANCE_MI, FAR_DISTANCE_MI,
    HIGH_CONFIDENCE_LETTER, HIGH_CONFIDENCE_NUMERIC,
)


class Verdict:
    """Outcome of evaluating the rules. `fired` names each rule that matched
    and the values that made it match, so a stored verdict can be read back
    later without re-running anything."""

    def __init__(self) -> None:
        self.fired: list[str] = []

    @property
    def must_flag(self) -> bool:
        return bool(self.fired)

    def fire(self, rule: str, detail: str) -> None:
        self.fired.append(f"{rule}: {detail}")

    def as_json(self) -> str:
        return json.dumps(self.fired)


def evaluate(conn: sqlite3.Connection) -> Verdict:
    """Evaluate every Fire flag rule against current collector data."""
    v = Verdict()
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=NEAREST_HOTSPOT_MAX_AGE_HOURS)).isoformat()

    # Rule 1 — any hotspot within 20 miles, regardless of confidence.
    # The confidence level is deliberately not consulted here; that
    # qualifier belongs to the 50-mile rule only.
    row = conn.execute(
        """
        SELECT distance_mi, confidence FROM hotspots
        WHERE collected_at >= ? AND distance_mi IS NOT NULL AND distance_mi <= ?
        ORDER BY distance_mi ASC LIMIT 1
        """,
        (cutoff, NEAR_DISTANCE_MI),
    ).fetchone()
    if row:
        v.fire("hotspot_within_20mi",
               f"{row['distance_mi']:.1f}mi, confidence={row['confidence']}")

    # Rule 2 — any high-confidence hotspot within 50 miles. The two encodings
    # of "high" are defined in thresholds.py alongside the distances.
    row = conn.execute(
        """
        SELECT distance_mi, confidence FROM hotspots
        WHERE collected_at >= ? AND distance_mi IS NOT NULL AND distance_mi <= ?
          AND (LOWER(confidence) = ?
               OR (CAST(confidence AS INTEGER) >= ?
                   AND confidence GLOB '[0-9]*'))
        ORDER BY distance_mi ASC LIMIT 1
        """,
        (cutoff, FAR_DISTANCE_MI, HIGH_CONFIDENCE_LETTER, HIGH_CONFIDENCE_NUMERIC),
    ).fetchone()
    if row:
        v.fire("high_confidence_within_50mi",
               f"{row['distance_mi']:.1f}mi, confidence={row['confidence']}")

    # Rule 3 — FRP rising across consecutive detections at the same location.
    # Hotspots dedup on (lat, lon, acq_date, acq_time, satellite), so a
    # re-detection of the same fire is a separate row; grouping by rounded
    # coordinates is what makes "the same hotspot over time" expressible.
    rows = conn.execute(
        """
        SELECT ROUND(latitude, 3) AS lat, ROUND(longitude, 3) AS lon,
               acq_date, acq_time, frp, distance_mi
        FROM hotspots
        WHERE collected_at >= ? AND frp IS NOT NULL
          AND distance_mi IS NOT NULL AND distance_mi <= ?
        ORDER BY lat, lon, acq_date, acq_time
        """,
        (cutoff, FAR_DISTANCE_MI),
    ).fetchall()
    prev_key = None
    prev_frp = None
    for r in rows:
        key = (r["lat"], r["lon"])
        if key == prev_key and prev_frp is not None and r["frp"] > prev_frp:
            v.fire("frp_rising",
                   f"{prev_frp:.2f} -> {r['frp']:.2f} MW at {r['distance_mi']:.1f}mi")
            break
        prev_key, prev_frp = key, r["frp"]

    # Rule 4 — a hotspot first seen since the previous observation.
    # collected_at is first-seen time (INSERT OR IGNORE dedup), so "arrived
    # since the last run" is exactly collected_at > that run's observed_at.
    last_obs = conn.execute(
        "SELECT observed_at FROM agent_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_obs:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(distance_mi) AS nearest FROM hotspots
            WHERE collected_at > ? AND distance_mi IS NOT NULL
              AND distance_mi <= ?
            """,
            (last_obs["observed_at"], FAR_DISTANCE_MI),
        ).fetchone()
        if row and row["n"]:
            v.fire("new_hotspot_since_last_run",
                   f"{row['n']} new, nearest {row['nearest']:.1f}mi")

    # Rule 5 — a named CAL FIRE incident actively burning inside the
    # unconditional radius. Every other rule requires a satellite detection,
    # and a small fire may never produce one: VIIRS pixels are 375m and the
    # overpasses are twice daily, so a ten-acre fire can burn without ever
    # reaching the hotspots table. The Steele Fire on 2026-09-09 started 14.1
    # miles from home and appeared in the incident feed 50 minutes later,
    # while nothing in these rules could have flagged on it.
    #
    # A confirmed, named, actively-burning incident is stronger evidence than
    # an unattributed thermal anomaly, so it earns the same unconditional
    # radius rather than a stricter one.
    #
    # Guarded separately: the incidents table does not exist until
    # incidents_collector.py has run, and a missing table must not take the
    # other rules down with it.
    try:
        row = conn.execute(
            """
            SELECT name, distance_mi, acres_burned, percent_contained
            FROM incidents
            WHERE is_active = 1 AND distance_mi IS NOT NULL
              AND distance_mi <= ?
            ORDER BY distance_mi ASC LIMIT 1
            """,
            (NEAR_DISTANCE_MI,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        contained = ("unknown" if row["percent_contained"] is None
                     else f"{row['percent_contained']:.0f}%")
        v.fire("active_incident_within_20mi",
               f"{row['name']} at {row['distance_mi']:.1f}mi, "
               f"{row['acres_burned']} acres, {contained} contained")

    # Rule 6 — the collector could not refresh. A data-quality flag, distinct
    # from a fire finding: it means the absence of hotspots is uninformative.
    row = conn.execute(
        "SELECT status, error_message FROM polls ORDER BY polled_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        v.fire("collector_never_polled", "no poll rows")
    elif row["status"] == "error":
        v.fire("collector_error", (row["error_message"] or "")[:120] or "status=error")

    return v


# The persistence exception in agent.py's prompt ("a previously-flagged
# low-confidence hotspot, unchanged across consecutive runs, need not be
# treated as newly alarming") is deliberately NOT implemented here.
#
# It is a de-escalation, so it cannot be expressed as another rule that fires
# — it would have to suppress rules 1-4, and "no change in character" is the
# one genuinely judgment-shaped clause in the whole prompt. Encoding it as
# arithmetic would be inventing a threshold the prompt does not state.
#
# The practical consequence: these rules will read as must_flag on persistent
# hotspots where the model has reasonably stopped flagging. That is expected,
# and is precisely the divergence worth measuring before anyone makes this
# enforcing — the shadow data will show how much of the disagreement is this
# one exception rather than a real miss.
