# Shared image for the four domain stacks (River, Weather, AQI, Fire) — they
# have identical dependencies (anthropic, httpx, mcp), so one image serves
# all four docker-compose services, distinguished only by working_dir and
# the command each is invoked with.
#
# node_config.json is deliberately NOT copied in here — it's bind-mounted
# at runtime (see docker-compose.yml) so this exact image is reusable across
# any node. Swap the mounted config, not the image, to deploy a new node.
FROM python:3.11-slim

WORKDIR /app

# All four stacks' requirements.txt are identical; River's is as good as any.
COPY River/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The shared agent runtime lives at the repo root and every domain agent
# imports it by adding /app to sys.path. Missing it breaks all four at import
# time — before logging is configured, and before record_failed_run is
# importable, so the failure it causes is also the failure that cannot be
# recorded. Third time a new root-level file has been left out of an image:
# publishers.json missed the Synthesis build twice already.
COPY agent_runtime.py ./

COPY River ./River
COPY Weather ./Weather
COPY AQI ./AQI
COPY Fire ./Fire
