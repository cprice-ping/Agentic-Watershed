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
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# One tool call's budget. Generous: on node-01 the server imports FastMCP in
# 0.69s and answers its heaviest query in 0.02s, so a run that reaches 30
# seconds is hung, not slow, and waiting longer would not help.
MCP_TIMEOUT_SECONDS = 30

# Two attempts, not more. The observed failure is a server that answers and
# then does not exit, which a second spawn clears; a fault that survives one
# retry is not transient and the run should end honestly rather than sit in
# cron for minutes.
MCP_ATTEMPTS = 2


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
        proc = subprocess.Popen(
            [sys.executable, str(server_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = proc.communicate(stdin_data,
                                              timeout=MCP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # communicate() does not kill on timeout, so without this the
            # hung server survives the agent and every timeout leaks one.
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            last_error = (f"{tool_name} timed out after {MCP_TIMEOUT_SECONDS}s "
                          f"(attempt {attempt} of {MCP_ATTEMPTS})")
            if log:
                log.warning("MCP %s", last_error)
            continue

        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == 1:
                content = response.get("result", {}).get("content", [])
                if content:
                    return content[0].get("text", "")

        # Answered, but not with anything usable. Not retried: a malformed
        # reply is a code fault, and a second identical spawn will produce a
        # second identical reply.
        if stderr and log:
            log.debug("MCP stderr: %s", stderr[:500])
        raise MCPUnavailable(
            f"{tool_name} returned no usable result"
            + (f"; stderr: {stderr[:200]}" if stderr else ""))

    raise MCPUnavailable(last_error or f"{tool_name} unavailable")


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
