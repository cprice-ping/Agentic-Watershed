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
    ])
