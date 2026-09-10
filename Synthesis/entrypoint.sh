#!/bin/sh
# Synthesis pipeline entrypoint
#
# Runs three steps in sequence on each container invocation:
#   1. subscriber.py  — pull observations published since the last run
#   2. agent_atproto.py — cross-domain synthesis
#   3. publisher.py   — post advisory to Bluesky
#
# SQLite note: Azure File Share uses SMB, which doesn't support the POSIX
# byte-range advisory locks that SQLite requires. All SQLite work runs in
# /tmp (local container storage, no locking issues). Persistent state is
# copied in from the file share at startup and back out at the end.
#
# /data (Azure File Share) is used only for copy-in / copy-out:
#   synthesis.db       — synthesis history (memory across runs)
#   synth_publisher.db — deduplication of published Bluesky posts
# subscriber.db is always rebuilt fresh from ATProto — no persistence needed.
# That is why LOOKBACK_HOURS below is the whole coverage of a run: nothing
# accumulates between runs, so the fetch window IS the window the agent sees.
#
# Environment variables required:
#   ANTHROPIC_API_KEY
#   BSKY_SYNTH_HANDLE
#   BSKY_SYNTH_APP_PASSWORD

set -e

DATA_DIR="${DATA_DIR:-/data}"
WORK_DIR="/tmp/synthesis"

# The one window. Passed to BOTH the subscriber (what it fetches) and the
# agent (what it reads), because they were two different numbers and the
# mismatch was invisible: on 2026-09-10 the subscriber fetched 15h while the
# agent was invoked with no flag, took its 24.0 default, and logged
# "Lookback: 24h" — a window a third larger than anything the database held.
#
# 27 rather than 15 so a single missed run cannot erase a domain. Watershed,
# weather and fire agents run every 12 hours, so a 15h window left three
# hours of slack: when the 2026-09-09 reboot cost watershed and weather one
# run each, both vanished from synthesis entirely and the advisory was built
# on aqi and fire alone. 27h tolerates one missed run per domain.
LOOKBACK_HOURS="${SYNTHESIS_LOOKBACK_HOURS:-27}"

mkdir -p "${WORK_DIR}"

echo "=== Synthesis pipeline starting at $(date -u) ==="
echo "Data directory : ${DATA_DIR}"
echo "Working directory: ${WORK_DIR}"

# ---------------------------------------------------------------------------
# Restore persistent state from file share into /tmp
# (safe even on first run — files may not exist yet)
# ---------------------------------------------------------------------------
echo ""
echo "--- Restoring state from persistent storage ---"
cp "${DATA_DIR}/synthesis.db"       "${WORK_DIR}/" 2>/dev/null && echo "  synthesis.db restored"       || echo "  synthesis.db not found (first run)"
cp "${DATA_DIR}/synth_publisher.db" "${WORK_DIR}/" 2>/dev/null && echo "  synth_publisher.db restored" || echo "  synth_publisher.db not found (first run)"

# ---------------------------------------------------------------------------
# Step 1: Subscribe
# Fetch observations published inside LOOKBACK_HOURS by trusted node DIDs.
# subscriber.db is ephemeral — rebuilt fresh on every run.
# ---------------------------------------------------------------------------
echo ""
echo "--- [1/3] Subscribe: fetching observations from ATProto ---"
python /app/subscriber.py \
    --db "${WORK_DIR}/subscriber.db" \
    --lookback "${LOOKBACK_HOURS}"

# ---------------------------------------------------------------------------
# Step 2: Synthesise
# ---------------------------------------------------------------------------
echo ""
echo "--- [2/3] Synthesise: cross-domain reasoning ---"
python /app/agent/agent_atproto.py \
    --subscriber-db "${WORK_DIR}/subscriber.db" \
    --synthesis-db  "${WORK_DIR}/synthesis.db" \
    --lookback "${LOOKBACK_HOURS}"

# ---------------------------------------------------------------------------
# Step 3: Publish
# ---------------------------------------------------------------------------
echo ""
echo "--- [3/3] Publish: posting advisory to Bluesky ---"
python /app/publisher.py \
    --synthesis-db "${WORK_DIR}/synthesis.db"

# ---------------------------------------------------------------------------
# Save persistent state back to file share
# ---------------------------------------------------------------------------
echo ""
echo "--- Saving state to persistent storage ---"
cp "${WORK_DIR}/synthesis.db"       "${DATA_DIR}/" && echo "  synthesis.db saved"
# synth_publisher.db is only created if the publisher actually ran (not dry-run, credentials set)
if [ -f "${WORK_DIR}/synth_publisher.db" ]; then
    cp "${WORK_DIR}/synth_publisher.db" "${DATA_DIR}/" && echo "  synth_publisher.db saved"
else
    echo "  synth_publisher.db not present (dry-run or nothing to publish)"
fi

echo ""
echo "=== Synthesis pipeline complete at $(date -u) ==="
