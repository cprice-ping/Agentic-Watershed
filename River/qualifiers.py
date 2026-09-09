"""
USGS qualifier codes — River
----------------------------
Every USGS instantaneous value arrives with a list of qualifier codes, and
the collector stores them joined into one string. They are two different
kinds of thing wearing the same shape:

  * review status  — P, A. These say whether USGS has checked the number
    yet. Essentially every real-time reading is P, so P carries no
    information about the river.

  * condition      — Ice, Eqp, Bkw, Dry, Fld, e, and the rest. These say
    something happened to the measurement. A gage that is ice-affected or
    whose equipment is malfunctioning is still returning a number, and the
    number is wrong in a way the value alone cannot show.

Conflating the two is how the collector log ended up printing ⚠️ against
every single reading: the marker fired on `P`, so it fired always, so it
meant nothing — and `Ice` looked exactly like a routine provisional value.

This module is the single definition of that split, read by collector.py
(for its log line) and mcp_server.py (which annotates every row it hands the
agent). The agent had no way to know what these codes meant: the tool output
carried the bare letter and no glossary appeared in any docstring or prompt.

Unrecognised codes pass through verbatim and count as notable. Not guessing
is the point — the same rule Weather/collector.py applies to an unfamiliar
unitCode. A code we cannot explain is exactly the one worth surfacing.
"""

# Codes that describe USGS's review state rather than the measurement.
# Deliberately small: anything not in here is treated as saying something
# about the water or the instrument.
ROUTINE = {"P", "A"}

# Standard NWIS qualifier codes. `e` is here rather than in ROUTINE on
# purpose — an estimated value is not a measurement, so it belongs with the
# conditions even though it is common.
MEANINGS = {
    "P": "provisional, subject to revision",
    "A": "approved for publication",
    "e": "value estimated",
    "Ice": "ice affected",
    "Eqp": "equipment malfunction",
    "Bkw": "backwater affected",
    "Fld": "value affected by flooding",
    "Dry": "dry — no water at the gage",
    "Zfl": "zero flow",
    "Mnt": "maintenance in progress",
    "Dis": "data collection discontinued",
    "Rat": "rating being developed or revised",
    "Ssn": "parameter monitored seasonally",
    "***": "temporarily unavailable",
}


def split(qualifier: str | None) -> list[str]:
    """Split a stored qualifier string back into its codes.

    The collector joins USGS's list with commas; this is the inverse. Empty
    and None both give an empty list so callers need no None check.
    """
    if not qualifier:
        return []
    return [c.strip() for c in qualifier.split(",") if c.strip()]


def is_notable(qualifier: str | None) -> bool:
    """True if any code says something about the measurement.

    A reading qualified only P or A is a normal reading — that is the whole
    reason this predicate exists rather than `bool(qualifier)`.
    """
    return any(code not in ROUTINE for code in split(qualifier))


def describe(qualifier: str | None) -> str | None:
    """Render the codes as readable text, or None if there are none.

    An unknown code is rendered as itself with no gloss invented for it, so
    a reader can tell "we know this means ice" from "USGS sent something
    this glossary has not seen".
    """
    codes = split(qualifier)
    if not codes:
        return None
    return "; ".join(
        f"{code} ({MEANINGS[code]})" if code in MEANINGS else f"{code} (unrecognised code)"
        for code in codes
    )
