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

# All three, not only the two loaded today. _load_domain_thresholds takes the
# domain as an argument and resolves BASE / domain / "thresholds.py", so the
# image can be asked for any of them; shipping two of three makes the third a
# latent crash the moment someone adds a line. check_image_files.py found
# this — AQI/thresholds.py was reachable and absent.
COPY Fire/thresholds.py ./Fire/thresholds.py
COPY Weather/thresholds.py ./Weather/thresholds.py
COPY AQI/thresholds.py ./AQI/thresholds.py

# Same reason, third instance: publisher.py imports agent_runtime for the note
# marker that keeps notes out of flagReason. Adding a root-level import to a
# file in an image that copies only its own directory breaks that image on
# import, and nothing on the Pi would show it — cron runs from a checkout
# where every path resolves.
#
# That static check now exists: check_image_files.py at the repo root.
COPY agent_runtime.py ./agent_runtime.py

# geo.py replaced this file's own copy of the haversine formula, whose comment
# used to say the publisher "cannot import it" — true until this image began
# copying repo-root modules. check_image_files.py caught its absence here
# before the image was ever built.
COPY geo.py ./geo.py
