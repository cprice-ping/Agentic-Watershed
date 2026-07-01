# Synthesis agent container image
#
# Runs the three-step pipeline on each invocation:
#   subscriber.py  → fetch observations from ATProto (fetch mode, exits when done)
#   agent_atproto.py → cross-domain synthesis with Claude Sonnet
#   publisher.py   → post advisory to Bluesky as napasynth01
#
# /data is a persistent mount — use an Azure File Share so state
# survives across container restarts (subscriber.db, synthesis.db, synth_publisher.db).
#
# Required environment variables at runtime:
#   ANTHROPIC_API_KEY
#   BSKY_SYNTH_HANDLE
#   BSKY_SYNTH_APP_PASSWORD

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY subscriber.py .
COPY publisher.py .
COPY synthesis_config.json .
COPY agent/ agent/

# Entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# /data is mounted as Azure File Share — declare as a volume so Docker
# surfaces it clearly, but the actual mount is configured in the ACI/Container Apps spec.
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
