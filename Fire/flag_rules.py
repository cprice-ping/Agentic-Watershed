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

Two output channels, not one. `fire()` forces a flag; `note()` records
something measured that is not a reason to alarm. The second was added on
2026-09-12 after the divergence data showed the newness rule firing on
essentially every run, and after it became clear that the reason it had to be
a flag was that there was nowhere else to put it.

Windows come from thresholds.py, the same module mcp_server.py reads, so a
divergence means the model and the rules disagreed about the same facts rather
than about which rows were in scope.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The note channel's marker, defined once at the repo root because
# shadow_report.py has to recognise exactly what this module writes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_runtime import NOTE_PREFIX  # noqa: E402

# Every threshold comes from thresholds.py, which agent.py's prompt,
# mcp_server.py's queries and ATProto/publisher.py's currency window are also
# read from. The currency window in particular used to exist in three copies
# and be absent from a fourth place that needed it.
from thresholds import (  # noqa: E402
    NEAREST_HOTSPOT_MAX_AGE_HOURS, NEAR_DISTANCE_MI, FAR_DISTANCE_MI,
    HIGH_CONFIDENCE_LETTER, HIGH_CONFIDENCE_NUMERIC,
    FRP_NOTABLE_PERCENTILE, FRP_RISE_MIN_FACTOR, notable_frp_at,
    NEW_LOCATION_RADIUS_MI, NEW_LOCATION_LOOKBACK_DAYS,
)

# The same great-circle distance the collector used to fill distance_mi, so
# "one kilometre apart" means the same thing here as in that column.
from collector import haversine_mi  # noqa: E402


def _notable_frp_threshold(conn: sqlite3.Connection) -> float | None:
    """The FRP value at FRP_NOTABLE_PERCENTILE of this collector's history.

    Its own history, never the region's — the same caveat the lexicon and
    mcp_server carry. Mirrors mcp_server's calculation so the rule and the
    tool call the same readings notable; they read the same column, so a
    divergence here would be the two disagreeing about arithmetic rather than
    about the fire.

    None when there is not enough history to place a reading, in which case
    the calling rule drops the percentile gate rather than inventing a
    threshold — the proportional gate still applies.
    """
    values = [r[0] for r in conn.execute(
        "SELECT frp FROM hotspots WHERE frp IS NOT NULL ORDER BY frp ASC")]
    return notable_frp_at(values)


class Verdict:
    """Outcome of evaluating the rules, on two channels.

    `fired` names each rule that matched and the values that made it match, so
    a stored verdict can be read back later without re-running anything. Only
    these force a flag.

    `notes` records something measured and worth keeping that is not by itself
    a reason to alarm. See agent_runtime.NOTE_PREFIX for why the second
    channel exists: without it, the only way to record a fact was to raise an
    alert about it, and Fire's newness rule spent two months doing exactly
    that.
    """

    def __init__(self) -> None:
        self.fired: list[str] = []
        self.notes: list[str] = []

    @property
    def must_flag(self) -> bool:
        return bool(self.fired)

    def fire(self, rule: str, detail: str) -> None:
        self.fired.append(f"{rule}: {detail}")

    def note(self, rule: str, detail: str) -> None:
        self.notes.append(f"{NOTE_PREFIX}{rule}: {detail}")

    def as_json(self) -> str:
        return json.dumps(self.fired + self.notes)


# A degree of latitude is about 69 miles everywhere, so this bound can only
# over-select: anything inside the radius is also inside the band. It keeps
# the haversine off the great majority of rows.
_LAT_BAND = NEW_LOCATION_RADIUS_MI / 69.0


def same_place(lat: float, lon: float, points) -> bool:
    """Whether (lat, lon) is within NEW_LOCATION_RADIUS_MI of any of *points*.

    One definition of "the same place", shared by the newness note and the FRP
    rule. They previously disagreed: newness compared by distance while
    frp_rising rounded coordinates to three decimals, and the two answers were
    not close — the same two months of detections are 86 places by distance
    and 337 by rounding.
    """
    for plat, plon in points:
        if abs(lat - plat) > _LAT_BAND:
            continue
        if haversine_mi(lat, lon, plat, plon) <= NEW_LOCATION_RADIUS_MI:
            return True
    return False


def group_by_place(rows) -> list[list]:
    """Cluster detections into places, preserving each row's order within one.

    Greedy, against each cluster's first member rather than all of them, so a
    long fire front cannot chain into one cluster spanning miles: every
    cluster stays inside a radius of its seed. Rows keep the order they
    arrive in, so passing them in acquisition order yields a time series per
    place.
    """
    clusters: list[list] = []
    seeds: list[tuple] = []
    for r in rows:
        lat, lon = r["latitude"], r["longitude"]
        for i, seed in enumerate(seeds):
            if same_place(lat, lon, [seed]):
                clusters[i].append(r)
                break
        else:
            seeds.append((lat, lon))
            clusters.append([r])
    return clusters


def new_locations(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Detections after *since* at places with no recent detection history.

    A place, not a row. The same fire re-observed on the next overpass is a
    new row — the dedup key includes acq_time and satellite — and treating
    that as an arrival is what made the old rule fire on 101 of ~118 runs.

    Shared with mcp_server.get_new_locations so the model and the shadow
    verdict count the same things. Two copies of this arithmetic would have
    the tool telling the agent one number while the recorded note said
    another, which is the drift thresholds.py exists to prevent.

    Returns one entry per distinct new place, nearest first.

    Places, not fires, and the difference is deliberate. Ten detections of one
    ignition inside a kilometre collapse to one entry, but a fire front
    genuinely spanning several kilometres reports several — ten VIIRS pixels
    in a line already cover about 3.75km. So the count carries a rough sense
    of extent rather than a count of ignitions, and a large new burn reads as
    a bigger number than a single hotspot. Anything reading this should treat
    it as "how much new ground", not "how many new fires".
    """
    lookback = (datetime.now(timezone.utc)
                - timedelta(days=NEW_LOCATION_LOOKBACK_DAYS)).isoformat()

    arrived = conn.execute(
        """
        SELECT latitude, longitude, distance_mi, confidence, frp, collected_at
        FROM hotspots
        WHERE collected_at > ? AND distance_mi IS NOT NULL AND distance_mi <= ?
        ORDER BY collected_at
        """,
        (since, FAR_DISTANCE_MI),
    ).fetchall()
    if not arrived:
        return []

    # Deliberately not distance-filtered. A prior detection just outside the
    # 50-mile band still means a place is not new, and excluding those would
    # invent arrivals along the boundary.
    known = conn.execute(
        """
        SELECT latitude, longitude FROM hotspots
        WHERE collected_at <= ? AND collected_at >= ?
        """,
        (since, lookback),
    ).fetchall()
    known_pts = [(r["latitude"], r["longitude"]) for r in known]

    out: list[dict] = []
    for r in arrived:
        lat, lon = r["latitude"], r["longitude"]
        if same_place(lat, lon, known_pts):
            continue
        # Also against places already accepted from this batch, so a cluster
        # of ten detections of one new fire counts as one place rather than
        # ten. The prompt asks about a cluster; this is what makes counting
        # one possible.
        if same_place(lat, lon, [(o["latitude"], o["longitude"]) for o in out]):
            continue
        out.append({
            "latitude": lat,
            "longitude": lon,
            "distance_mi": r["distance_mi"],
            "confidence": r["confidence"],
            "frp": r["frp"],
            "first_seen": r["collected_at"],
        })
    out.sort(key=lambda o: o["distance_mi"])
    return out


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

    # Rule 3 — FRP rising materially at the same location, to a level that is
    # itself unusual for this collector.
    #
    # Hotspots dedup on (lat, lon, acq_date, acq_time, satellite), so a
    # re-detection of the same fire is a separate row; grouping detections
    # into places is what makes "the same hotspot over time" expressible.
    #
    # That grouping used to round coordinates to three decimals — about 110m,
    # finer than the instrument can place a pixel. VIIRS is 375m at nadir and
    # nearer 800m at the swath edge, with geolocation error on top, so one
    # fire lands in a different cell from pass to pass: the same two months of
    # detections are 86 places measured by distance and 337 by rounding. The
    # rule could therefore only see a fire growing when two consecutive passes
    # happened to fall in the same 110m box, which is the opposite failure
    # from the newness rule's and the more dangerous direction — it hid
    # intensification rather than inventing it. Places now come from
    # group_by_place, the same definition the newness note uses.
    #
    # It also used to fire on ANY rising consecutive pair inside the 72-hour
    # window, first match wins. With fragmented grouping that rarely found a
    # pair at all; with correct grouping each place carries a full series and
    # "some pair rose at some point in three days" would be close to always
    # true, and would keep firing for three days on a spike that had already
    # reversed. The comparison is now the latest reading at a place against
    # the one before it, which is the question the rule is named for: is this
    # fire intensifying now. Where several places qualify, the one with the
    # highest current FRP is reported.
    #
    # Two gates, added 2026-09-11. The rule used to fire on any increase and
    # therefore fired on 12 of the first 13 scored runs — on 0.09 -> 0.10 MW
    # at 43.3 miles, and on 9.19 -> 9.25 MW at 31.1 miles. Consecutive VIIRS
    # retrievals of one pixel vary by more than that for reasons unrelated to
    # the fire, so those were readings of the instrument. Both gates come from
    # thresholds.py: the rise must be proportionally material, and the new
    # value must sit in the top of this collector's own FRP history.
    #
    # Requiring the *new* value to be notable rather than the delta is
    # deliberate. A fire doubling from 0.1 to 0.2 MW has doubled and still
    # does not matter; one already among the largest this node has recorded,
    # and growing, is the case the rule is for.
    notable_frp = _notable_frp_threshold(conn)
    rows = conn.execute(
        """
        SELECT latitude, longitude, acq_date, acq_time, frp, distance_mi
        FROM hotspots
        WHERE collected_at >= ? AND frp IS NOT NULL
          AND distance_mi IS NOT NULL AND distance_mi <= ?
        ORDER BY acq_date, acq_time
        """,
        (cutoff, FAR_DISTANCE_MI),
    ).fetchall()

    best = None
    for series in group_by_place(rows):
        if len(series) < 2:
            continue
        prev, latest = series[-2], series[-1]
        if not prev["frp"] or prev["frp"] <= 0:
            continue
        if latest["frp"] < prev["frp"] * FRP_RISE_MIN_FACTOR:
            continue
        if notable_frp is not None and latest["frp"] < notable_frp:
            continue
        if best is None or latest["frp"] > best[1]["frp"]:
            best = (prev, latest)

    if best is not None:
        prev, latest = best
        gate = (f">= p{FRP_NOTABLE_PERCENTILE:g} of {notable_frp:.2f} MW"
                if notable_frp is not None
                else "percentile gate skipped, <20 FRP readings on record")
        v.fire("frp_rising",
               f"{prev['frp']:.2f} -> {latest['frp']:.2f} MW "
               f"(x{latest['frp'] / prev['frp']:.2f}, {gate}) "
               f"at {latest['distance_mi']:.1f}mi")

    # Rule 4 — places with no recent detection history. A NOTE, not a flag.
    #
    # This was new_hotspot_since_last_run, and it fired on 101 of ~118 runs
    # over two months because it counted rows rather than places: collected_at
    # is first-seen time, but the dedup key includes acq_time and satellite,
    # so every overpass of an already-burning fire inserts one. In the fifteen
    # runs of the shadow window it fired fifteen times, and thirteen of those
    # had no detection within twenty miles at all. The two that did were the
    # September event, where hotspot_within_20mi fires regardless — so as a
    # flag it contributed nothing on the only occasions it could have.
    #
    # Demoted rather than deleted, because the signal is real and something
    # needs it. The prompt's persistence exception lets the model stop
    # re-alarming on a hotspot that is unchanged across runs, and nothing in
    # the system could tell it what had changed: get_hotspots_since and
    # get_hotspot_count_since are both row-based and inherit the same defect.
    # A count of genuinely new places is the input that exception was always
    # missing, and it is not an alarm.
    last_obs = conn.execute(
        "SELECT observed_at FROM agent_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_obs:
        fresh = new_locations(conn, last_obs["observed_at"])
        if fresh:
            v.note("new_locations",
                   f"{len(fresh)} place(s) with no detection within "
                   f"{NEW_LOCATION_RADIUS_MI:g}mi in the last "
                   f"{NEW_LOCATION_LOOKBACK_DAYS}d, nearest "
                   f"{fresh[0]['distance_mi']:.1f}mi")

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
