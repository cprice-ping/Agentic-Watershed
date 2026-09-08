# Publisher only needs httpx — no anthropic, no mcp. Separate, lighter image
# from the domain stacks' Dockerfile (repo root) since it has no reasoning
# to do, just reads the domain DBs and writes lexicon records.
#
# Build context is the repo root (not ATProto/), same reason as the root
# Dockerfile: publisher.py reads node_config.json from its parent directory
# (BASE = Path(__file__).parent.parent) and, at runtime, each domain's
# data/*.db via bind mount — see docker-compose.yml.
FROM python:3.11-slim

WORKDIR /app

COPY ATProto/requirements.txt ./ATProto/requirements.txt
RUN pip install --no-cache-dir -r ATProto/requirements.txt

COPY ATProto ./ATProto

# publisher.py reads these from BASE (= /app) at import time: node_config.json
# for the node identity and PDS, and each domain's thresholds.py for the query
# windows it must share with that domain's agent. Neither was copied before,
# so this image crashed on import — cron on the Pi runs from a full checkout
# and never hit it.
COPY node_config.json ./node_config.json
COPY Fire/thresholds.py ./Fire/thresholds.py
