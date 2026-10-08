"""
Shared agent runtime — MCP calls and run outcomes
-------------------------------------------------
Two things every domain agent needs and all four had their own copy of.

The MCP client is a stdio subprocess per tool call. That design is fine, but
the copies all called `proc.communicate(..., timeout=30)` bare: a timeout
raised straight out of `gather_context()`, killed the run, left no record,
and — because `communicate` does not kill on timeout — left the subprocess
behind. On 2026-09-10 the Weather agent hit exactly that. It had already
fetched one tool's output successfully, threw it away, and the domain simply
stopped appearing in synthesis for two days.

Nothing reported it. The publisher logged "[weather] Nothing new to publish"
on every run, which is what it also logs for a domain that ran fine and had
nothing new. A crashed agent and a quiet one were indistinguishable from the
outside, and that is the actual defect — the timeout was only the trigger.

Living at the repo root rather than in each domain because four copies of a
constant is how the flag thresholds drifted (CONTEXT.md, 2026-09-08). Pure
standard library, so the per-domain venvs need nothing new.
"""

import json
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# One tool call's budget. Generous: on node-01 the server imports FastMCP in
# 0.69s and answers its heaviest query in 0.02s, so a run that reaches 30
# seconds is hung, not slow, and waiting longer would not help.
MCP_TIMEOUT_SECONDS = 30

# Two attempts, not more. A fault that survives one retry is not transient
# and the run should end honestly rather than sit in cron for minutes. (The
# failure this was first sized for — a server that answers and then does not
# exit — no longer costs an attempt: the reply is returned as soon as it
# arrives, and a server that then lingers is killed.)
MCP_ATTEMPTS = 2

# How long a server gets to exit after its stdin is closed, once the reply is
# in hand, before it is killed.
MCP_EXIT_GRACE_SECONDS = 5


def compact_json(obj) -> str:
    """JSON for a model to read, without the whitespace a human would want.

    `json.dumps(indent=2)` spends roughly 30% of a tool payload on spaces and
    newlines — measured on River's hourly series, 51,118 characters against
    35,801 compact. A model reads either form equally well, so the pretty
    version was 3,800 tokens per River run of pure indentation.

    Kept out of the offline tools deliberately: extract_training_data.py and
    the reports are read by people, where the indentation earns its cost.
    Use `python -m json.tool` when inspecting a tool by hand.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class MCPUnavailable(RuntimeError):
    """A tool could not be reached after every attempt.

    Raised rather than returning a sentinel string because the caller must
    not carry on and reason over a context with a hole in it. The four agents
    previously returned "[Tool call failed: name]" for a malformed reply,
    which reads as data and reaches the model as if it were evidence.
    """


def call_mcp_tool(server_path: Path, tool_name: str,
                  arguments: dict | None = None,
                  client_name: str = "watershed-agent",
                  log=None) -> str:
    """Call one MCP tool over stdio, retrying once on timeout.

    Returns the tool's text result. Raises MCPUnavailable if every attempt
    times out.
    """
    arguments = arguments or {}
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    init_request = {
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1.0"},
        },
    }
    stdin_data = (
        json.dumps(init_request) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                      "params": {}}) + "\n"
        + json.dumps(request) + "\n"
    )

    last_error = None
    for attempt in range(1, MCP_ATTEMPTS + 1):
        try:
            reply, stdout, stderr, returncode = _exchange(
                server_path, stdin_data, MCP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            last_error = (f"{tool_name} timed out after {MCP_TIMEOUT_SECONDS}s "
                          f"(attempt {attempt} of {MCP_ATTEMPTS})")
            if log:
                log.warning("MCP %s", last_error)
            continue

        if reply is not None:
            content = (reply.get("result") or {}).get("content") or []
            if content:
                return content[0].get("text", "")

        # Answered, but not with anything usable. Not retried: a malformed
        # reply is a code fault, and a second identical spawn will produce a
        # second identical reply.
        #
        # Say which of the three failures this was. All three used to read
        # "no usable result" plus the first 200 characters of stderr — which
        # is always the server's "Processing request" banner, so the one line
        # of evidence shown was the one line that never varies. The tail is
        # where a traceback ends up.
        if reply is None:
            what = (f"no reply to the call (server exit code {returncode}, "
                    f"{len(stdout)} bytes on stdout, last: {stdout[-200:]!r})")
        elif reply.get("error"):
            err = reply["error"]
            what = f"a JSON-RPC error: {err.get('code')} {err.get('message')}"
        else:
            what = "an empty result"
        if stderr and log:
            log.debug("MCP stderr: %s", stderr[-2000:])
        raise MCPUnavailable(
            f"{tool_name} returned {what}"
            + (f"; stderr tail: {stderr.strip()[-300:]}" if stderr.strip() else ""))

    raise MCPUnavailable(last_error or f"{tool_name} unavailable")


def _exchange(server_path: Path, stdin_data: str, timeout: float):
    """Start one server, send it stdin_data, and read until the reply to id 1.

    Returns (reply or None, stdout read, stderr, exit code). Raises
    subprocess.TimeoutExpired if neither a reply nor end-of-output arrives
    within `timeout`.

    Stdin stays open until the reply has been read. This used to be
    `communicate(stdin_data)`, which writes and closes stdin at once — and
    the MCP server treats a closed stdin as the client leaving and cancels
    every request still in flight ("Transport closed: cancel in-flight
    handlers", mcp/server/lowlevel/server.py; present in every release from
    1.27, the oldest requirements.txt allows). Whether the reply got out
    before the cancel was a race. On the Pi it was won for months; in the
    first container build, on a laptop, it was lost by three of four agents
    on 2026-10-07 — server exit 0, nothing on stdout but its answer to
    `initialize`. Why the timing differed there is not established. What is:
    a tool that yields to the event loop for 5 ms loses every time, and even
    a zero-length yield loses about one call in five.

    Read line by line from the stream, which splits on newlines only — never
    str.splitlines(), which also splits on U+0085, U+2028 and U+2029. JSON
    leaves those three unescaped, so a reply containing one was cut into
    fragments that each failed to parse.
    """
    proc = subprocess.Popen(
        [sys.executable, str(server_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    lines: queue.Queue = queue.Queue()
    err_parts: list[str] = []

    # Both pipes drained on their own threads: an undrained stderr that fills
    # its buffer blocks the server mid-write, which would look like a hang.
    def pump_stdout():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def pump_stderr():
        err_parts.append(proc.stderr.read())

    pumps = [threading.Thread(target=pump_stdout, daemon=True),
             threading.Thread(target=pump_stderr, daemon=True)]
    for pump in pumps:
        pump.start()

    out: list[str] = []
    reply = None
    timed_out = False
    try:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.flush()
        except BrokenPipeError:
            pass        # server already gone; its output and exit code say why
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = lines.get(timeout=max(remaining, 0))
            except queue.Empty:
                timed_out = True
                break
            if line is None:
                break   # server closed stdout without replying
            out.append(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == 1:
                reply = message
                break
    finally:
        # Only now is the server told we are done.
        try:
            proc.stdin.close()
        except OSError:
            pass
        # A server that timed out gets no grace: it has already had the whole
        # budget. One that replied gets a moment to exit on its own, then is
        # killed rather than waited on forever — without that, a hung server
        # survives the agent, and every hang leaks one.
        try:
            proc.wait(timeout=0 if timed_out else MCP_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for pump in pumps:
            pump.join(timeout=MCP_EXIT_GRACE_SECONDS)

    if timed_out and reply is None:
        raise subprocess.TimeoutExpired(proc.args, timeout)
    return reply, "".join(out), "".join(err_parts), proc.returncode


# ---------------------------------------------------------------------------
# Run outcomes
# ---------------------------------------------------------------------------

def ensure_run_columns(conn: sqlite3.Connection) -> None:
    """Add status/error to agent_observations if this DB predates them.

    Existing rows keep status NULL, which readers must treat as success —
    every row written before this existed was a completed run, since a failed
    run wrote nothing at all.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_observations)")}
    for name, decl in (("status", "TEXT"), ("error", "TEXT")):
        if name not in cols:
            conn.execute(
                f"ALTER TABLE agent_observations ADD COLUMN {name} {decl}")
    conn.commit()


def record_failed_run(db_path: Path, reason: str, model: str | None = None,
                      log=None) -> None:
    """Write a row saying this run failed, so the gap has a cause attached.

    Deliberately writes an observation row rather than only logging. A log
    line lives on one machine and is read by a human who already suspects
    something; this row is read by the publisher, which is what noticed
    nothing for two days.

    `summary` is NOT NULL in every domain's schema, so the reason goes there
    as well as in `error` — a human reading the table sees what happened
    without joining anything. `flagged` stays 0: this is an absence of
    assessment, not an assessment of danger, and a failed run must never be
    able to raise an alert it never computed.

    Best-effort by construction. This runs on a path where something has
    already gone wrong, so it must never raise and mask the original fault.
    """
    try:
        conn = sqlite3.connect(db_path)
        ensure_run_columns(conn)
        conn.execute(
            """INSERT INTO agent_observations
               (observed_at, summary, flagged, reasoning, model, status, error)
               VALUES (?, ?, 0, ?, ?, 'failed', ?)""",
            (datetime.now(timezone.utc).isoformat(),
             f"Agent run failed: {reason}",
             "No reasoning: the run did not reach the model.",
             model, reason),
        )
        conn.commit()
        conn.close()
        if log:
            log.error("Recorded failed run: %s", reason)
    except sqlite3.Error as exc:          # pragma: no cover - defensive
        if log:
            log.error("Could not even record the failure (%s): %s", reason, exc)


def is_failed_row(row) -> bool:
    """Whether a row records a failed run rather than an observation.

    NULL status means a row written before the column existed, which is a
    successful run by definition — a failed one wrote nothing back then.
    """
    try:
        return (row["status"] or "").lower() == "failed"
    except (IndexError, KeyError, TypeError):
        return False


# ---------------------------------------------------------------------------
# The note channel
# ---------------------------------------------------------------------------
#
# A flag_rules Verdict has two outputs, not one. `fire()` records something
# that forces flagged=true; `note()` records something worth having in the
# shadow record that is not by itself a reason to alarm.
#
# The second channel exists because its absence caused a real defect. Fire's
# new_hotspot_since_last_run fired on 101 of ~118 runs across two months, and
# on 15 of the 15 runs in the shadow window — 13 of which had no detection
# within 20 miles at all. A rule that cannot be silent carries no information,
# the same failure frp_rising had. But the underlying signal is not worthless:
# it is exactly what the prompt's persistence exception needs as input, since
# "this hotspot is unchanged from last run" cannot be judged without knowing
# what changed. There was nowhere to put a fact that is informative and not
# alarming, so it became an alarm.
#
# Notes travel in the same `rules_fired` JSON list as fired rules, marked with
# this prefix, rather than in a new column or a changed JSON shape. Two months
# of recorded verdicts already exist and stay readable: no historical entry
# begins with "note:", so the split is unambiguous in both directions.
NOTE_PREFIX = "note:"


def is_note(entry: str) -> bool:
    """Whether a rules_fired entry is a note rather than a fired rule."""
    return str(entry).startswith(NOTE_PREFIX)


def strip_note(entry: str) -> str:
    """A note entry without its marker, for display."""
    e = str(entry)
    return e[len(NOTE_PREFIX):] if is_note(e) else e


def rule_name(entry: str) -> str:
    """The rule identifier from a fired-rule or note entry."""
    return strip_note(entry).split(":")[0].strip()


def consecutive_runs_fired(conn, rules: list[str], limit: int = 40,
                           max_gap_hours: float = 36.0) -> dict:
    """For each rule name, how many consecutive prior runs it also fired on.

    Counting back from the most recent recorded observation, stopping at the
    first run where a rule did not fire. Failed runs are skipped rather than
    breaking a streak — an agent that crashed did not evaluate anything, and
    treating that as "the rule stopped firing" would reset the count on one
    bad run.

    A gap does break it. If two evaluations — or the newest one and now — are
    more than `max_gap_hours` apart, the count stops there: nothing was
    evaluated in between, so continuity across it is not something the node
    observed. Without this, the count ran straight through node-01's two-week
    outage: on 2026-10-08 a synthesis advisory called a 17.3 mi ESE hotspot
    "unchanged in location" over five consecutive runs, when three of the
    five were in September and about a different detection, 17.9 mi WSW.
    36 hours lets a twice-daily agent miss two runs before the streak ends.

    This exists because a rule that fires forever is indistinguishable from
    one that just started. On 2026-09-13 a synthesis advisory reported Fire's
    rules-only divergence as "the one flag-worthy item this run" — correctly,
    the first time. But the Steele Fire sits inside the unconditional radius
    and is in the incident feed, so those rules will fire every twelve hours
    until it drops out, while the domain model correctly stops flagging under
    its persistence exception. Left alone, that becomes an alarm that cannot
    fall silent, one layer above the node.

    Duration is reported rather than significance. The node says how long a
    rule has been firing; whether twelve runs of the same thing is news stays
    the consumer's call. Synthesis deliberately does not import node
    thresholds — a grader that adopts the gradee's definition of success
    measures nothing — and "ignore this divergence" would be exactly that.
    """
    counts = {r: 0 for r in rules}
    if not rules:
        return counts
    try:
        rows = conn.execute(
            "SELECT rules_fired, status, observed_at FROM agent_observations "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        try:
            rows = conn.execute(
                "SELECT rules_fired, observed_at FROM agent_observations "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except Exception:
            return counts

    max_gap = max_gap_hours * 3600.0
    newer = datetime.now(timezone.utc)
    live = {r for r in rules}
    for row in rows:
        if not live:
            break
        if is_failed_row(row):
            continue
        at = _parse_iso(row["observed_at"])
        # An undated run cannot show continuity either; stop rather than
        # assume it.
        if at is None or (newer - at).total_seconds() > max_gap:
            break
        newer = at
        try:
            fired = {rule_name(e) for e in json.loads(row["rules_fired"] or "[]")
                     if not is_note(e)}
        except (TypeError, ValueError):
            fired = set()
        for r in list(live):
            if r in fired:
                counts[r] += 1
            else:
                live.discard(r)
    return counts


def _parse_iso(ts):
    try:
        t = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    try:
        rows = conn.execute(
            "SELECT rules_fired, status FROM agent_observations "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        try:
            rows = conn.execute(
                "SELECT rules_fired FROM agent_observations "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except Exception:
            return counts

    live = {r for r in rules}
    for row in rows:
        if not live:
            break
        if is_failed_row(row):
            continue
        try:
            fired = {rule_name(e) for e in json.loads(row["rules_fired"] or "[]")
                     if not is_note(e)}
        except (TypeError, ValueError):
            fired = set()
        for r in list(live):
            if r in fired:
                counts[r] += 1
            else:
                live.discard(r)
    return counts
