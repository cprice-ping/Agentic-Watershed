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

COPY River ./River
COPY Weather ./Weather
COPY AQI ./AQI
COPY Fire ./Fire
