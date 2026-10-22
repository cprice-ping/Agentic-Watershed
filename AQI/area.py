"""
Which AirNow monitors count as this node's regional reference
-------------------------------------------------------------
AirNow's current-observations service returns the closest monitor with a
reading, and when the usual one misses an hour it reaches further: on
2026-10-10 Napa's PM2.5 came from Davis, in the Sacramento Valley, about 45
miles east. A reading from there describes Davis.

node_config.json names the monitors accepted for this node
(aqi.accepted_monitors). Every reading is still stored, but each is marked:

    in_area = 1   from an accepted monitor
    in_area = 0   from any other monitor — kept, never used for a value,
                  a trend or a flag
    in_area NULL  monitor not recorded (rows from before 2026-10-08)

and every consumer reads through the view observations_in_area. When the
accepted monitors are all silent, the node therefore reports no current
reading — the data-age fields say how old the newest accepted one is —
rather than a borrowed one. An honest gap over a misplaced number.

Classification is recomputed on every ensure(), so changing the list in
node_config.json applies to history too.
"""

import json
import sqlite3
from pathlib import Path

_CFG = json.loads((Path(__file__).parent.parent / "node_config.json").read_text())
ACCEPTED = tuple(m.strip() for m in _CFG.get("aqi", {}).get("accepted_monitors", [])
                 if m and m.strip())

VIEW = "observations_in_area"


def in_area(site_name) -> int | None:
    """1, 0 or None for one reading's monitor. With no list configured,
    every recorded monitor is accepted — the behaviour before the list."""
    if not site_name or not str(site_name).strip():
        return None
    if not ACCEPTED:
        return 1
    return int(str(site_name).strip().lower() in {m.lower() for m in ACCEPTED})


def ensure(conn: sqlite3.Connection) -> None:
    """Add the in_area column if missing, classify every row against the
    current list, and create the view. Cheap: the table is small."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if "observations" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    if "site_name" not in cols:
        conn.execute("ALTER TABLE observations ADD COLUMN site_name TEXT")
    if "in_area" not in cols:
        conn.execute("ALTER TABLE observations ADD COLUMN in_area INTEGER")
    # Ozone was stored as "O3" by the retired AirNow endpoint and as "OZONE"
    # by the current one (KEPT_PARAMETERS). The two names made two series, and
    # the O3 one ended on 2026-09-21: get_current_aqi took its newest row as
    # a separate pollutant and reported ozone stale for weeks after it was
    # current again. One name, so one series.
    conn.execute("UPDATE observations SET parameter = 'OZONE' "
                 "WHERE parameter = 'O3'")
    if ACCEPTED:
        marks = ",".join("?" * len(ACCEPTED))
        conn.execute(
            f"""UPDATE observations SET in_area = CASE
                    WHEN site_name IS NULL OR TRIM(site_name) = '' THEN NULL
                    WHEN LOWER(TRIM(site_name)) IN ({marks}) THEN 1
                    ELSE 0 END""",
            [m.lower() for m in ACCEPTED])
    else:
        conn.execute("""UPDATE observations SET in_area = CASE
                            WHEN site_name IS NULL OR TRIM(site_name) = '' THEN NULL
                            ELSE 1 END""")
    # SQLite re-reads a view's SELECT * each time it is used, so columns
    # added to observations later appear in the view too.
    conn.execute(f"CREATE VIEW IF NOT EXISTS {VIEW} AS SELECT * FROM observations "
                 "WHERE in_area IS NULL OR in_area = 1")
    conn.commit()
