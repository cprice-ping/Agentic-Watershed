"""
Flag thresholds — Fire
----------------------
The single definition of what counts as a nearby hotspot at this node.
agent.py builds its prompt criteria from these, mcp_server.py queries with
them, flag_rules.py evaluates them, and ATProto/publisher.py loads the
currency window from here so a published record and the agent that wrote it
describe the same set of hotspots. See Weather/thresholds.py for why this
pattern exists.

The currency window is the one that has already bitten: publisher.py had no
window at all while get_nearest_hotspots did, so a five-day-old detection
was published as an 8.5-mile threat while the agent's own summary correctly
said nothing was inside 20 miles. Both sides now read the number from here.
"""

import json
from pathlib import Path

_NODE_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
_DAY_RANGE = _NODE_CFG["fire"]["day_range"]

# FIRMS itself is only ever queried for the last day_range days (collector.py),
# so a detection older than that plus a day of poll-timing slack cannot be
# current. A hotspot outside this window means "nothing current nearby", not
# "this old one is still the nearest threat".
NEAREST_HOTSPOT_MAX_AGE_HOURS = _DAY_RANGE * 24 + 24

# How many hours around observedAt the published hotspotCount covers. Narrower
# than the currency window and documented separately in the lexicon.
HOTSPOT_COUNT_WINDOW_HOURS = 6

NEAR_DISTANCE_MI = 20.0   # unconditional on confidence
FAR_DISTANCE_MI  = 50.0   # high-confidence only

# VIIRS encodes confidence as l/n/h; MODIS uses 0-100, where >=80 is the
# conventional high band. Both appear in the hotspots table.
HIGH_CONFIDENCE_LETTER  = "h"
HIGH_CONFIDENCE_NUMERIC = 80

# A reading at or above this percentile of the collector's own FRP history is
# worth calling unusual. Below it, it is an ordinary detection for this area
# however large the raw number looks.
#
# This exists because the agent had no way to judge one. On 2026-09-09 it
# described a 66 MW detection as "well beyond anything previously reported in
# FRP magnitude" when the same table held 64.0 MW two weeks earlier and 53.2
# MW at 9.8 miles before that — 66 was the highest of seven comparable events
# in two months, about 3% above the prior peak. The tools returned a raw FRP
# and no distribution to read it against, so the superlative was a guess.
FRP_NOTABLE_PERCENTILE = 95.0

# Matching a hotspot to a named CAL FIRE incident.
#
# An incident is published as a single point; a fire is an area. The Plaskett
# Fire was 29,884 acres when checked, which is 46.7 square miles — if roughly
# circular, its perimeter sits about 3.9 miles from any centre point. A flat
# radius would either miss large fires or swallow unrelated detections near
# small ones, so the tolerance grows with the burned area and only the slop in
# the point location itself is fixed.
INCIDENT_MATCH_BASE_MI = 5.0
ACRES_PER_SQ_MI = 640.0


# A hotspot must also fall inside the incident's burning period, not merely
# near its location. Distance alone matches a detection today against a fire
# that closed months ago: the first real poll on 2026-09-09 found five
# incidents within 17.6 miles of Napa, every one 100% contained, so a new
# detection near any of them would have been published as a known, handled
# event. That is the stale-record-makes-a-current-signal-look-understood
# failure, in the direction that hides a real fire.
#
# The window is padded at both ends, for opposite reasons. A satellite sees
# heat before an incident is reported and published, so a detection can
# legitimately precede the recorded start. And ground stays hot after
# containment, so a detection can legitimately follow the end.
INCIDENT_MATCH_LEAD_DAYS = 1.0    # detection before the incident was reported
INCIDENT_MATCH_TAIL_DAYS = 3.0    # residual heat after it closed


def incident_match_radius_mi(acres_burned) -> float:
    """How far from an incident's published point a hotspot may sit and still
    plausibly belong to it: the equivalent circular radius of the burned area,
    plus fixed slop for the point being a label rather than a centroid."""
    import math
    try:
        acres = float(acres_burned)
    except (TypeError, ValueError):
        return INCIDENT_MATCH_BASE_MI
    if acres <= 0:
        return INCIDENT_MATCH_BASE_MI
    return round(INCIDENT_MATCH_BASE_MI
                 + math.sqrt((acres / ACRES_PER_SQ_MI) / math.pi), 2)



def _n(value: float) -> str:
    return f"{value:g}"


def flag_criteria_text() -> str:
    """The distance-based flag criteria as prompt bullets, generated from the
    constants so the prompt cannot drift from what is enforced.

    Only the numeric criteria are generated. The remaining rules in the
    prompt — FRP rising, a new cluster since last run, collector error — are
    prose conditions with no threshold to share, and the persistence
    exception is deliberately not encoded at all (see flag_rules.py).
    """
    return "\n".join([
        f"- Any hotspot detected within {_n(NEAR_DISTANCE_MI)} miles of Napa "
        f"Valley center — this is\n  unconditional on confidence level. A "
        f"low-confidence detection within {_n(NEAR_DISTANCE_MI)}\n  miles "
        f"still counts; do not require elevated confidence for this specific\n"
        f"  trigger (that requirement only applies to the separate "
        f"{_n(FAR_DISTANCE_MI)}-mile rule below).",
        f"- Any high-confidence hotspot within {_n(FAR_DISTANCE_MI)} miles",
        f"- Any actively burning named CAL FIRE incident within "
        f"{_n(NEAR_DISTANCE_MI)} miles. This does not require a satellite\n"
        f"  detection and does not wait for one: a small fire may never "
        f"produce a\n  hotspot at all, and a confirmed active incident "
        f"inside the unconditional\n  radius is stronger evidence than an "
        f"unattributed thermal anomaly.",
    ])
