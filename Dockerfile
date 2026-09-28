# The node image: all four domain stacks (River, Weather, AQI, Fire), the
# ATProto publisher, and the scheduler that runs them. One image, one
# long-running `node` container — see docker-compose.yml for the rest of the
# node (the PDS and the Cloudflare tunnel).
#
# This used to be two images run one-shot from host cron. Moving the schedule
# into the container is what lets a node be `docker compose up -d` on any
# machine: the host's crontab and /etc/environment were the two pieces of
# node-01 that lived outside the repo, and node-01's SD card took both with it.
#
# node_config.json is deliberately NOT copied in here — it's bind-mounted
# at runtime (see docker-compose.yml) so this exact image is reusable across
# any node. Swap the mounted config, not the image, to deploy a new node.

# supercronic: cron built for containers. It passes the container's
# environment to every job (no /etc/environment sourcing to get wrong), logs
# each job's output and exit status to stdout, and will not start a job while
# the previous run of it is still going. Built from source through the Go
# module proxy rather than downloaded as a release binary: the build then
# works on any architecture (arm64 laptop, amd64 VM) with no per-arch URL and
# checksum to keep in step, and go.sum is verified against the checksum
# database. timetzdata embeds the zone database, so TZ resolves even if the
# runtime image's zoneinfo ever goes missing.
FROM golang:1.26-bookworm AS scheduler
RUN CGO_ENABLED=0 go install -tags timetzdata github.com/aptible/supercronic@v0.2.49

FROM python:3.11-slim

WORKDIR /app

# The four stacks' requirements.txt are identical, and the publisher's
# (httpx only) is a subset of them; River's is as good as any.
COPY River/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=scheduler /go/bin/supercronic /usr/local/bin/supercronic

# The shared agent runtime lives at the repo root and every domain agent
# imports it by adding /app to sys.path. Missing it breaks all four at import
# time — before logging is configured, and before record_failed_run is
# importable, so the failure it causes is also the failure that cannot be
# recorded. Third time a new root-level file has been left out of an image:
# publishers.json missed the Synthesis build twice already.
COPY agent_runtime.py ./

# Great-circle distance and bearing, shared by the Fire stack and the
# publisher so there is one definition rather than a copy per consumer.
COPY geo.py ./

COPY River ./River
COPY Weather ./Weather
COPY AQI ./AQI
COPY Fire ./Fire

# The publisher reads node_config.json (mounted) and each domain's
# thresholds.py from BASE = /app. It had its own image until the schedule
# moved in here; a separate image for one script would now need its own
# scheduler or a docker socket, and it already fits this one.
COPY ATProto ./ATProto

COPY run-job.sh node.crontab ./

# Scripts log as they go rather than when a buffer fills — otherwise a job
# that hangs shows nothing at all in `docker compose logs node`.
ENV PYTHONUNBUFFERED=1

# Which commit this image was built from, printed by run-job.sh at the start
# of every job. A checkout could be asked with `git rev-parse`; an image has
# no .git, and "which code is actually running" is the question that once
# went four weeks unanswered on the Pi. Set by the build command in
# DEPLOYMENT.md; a build that skips it says "unknown" rather than guessing.
ARG GIT_COMMIT=unknown
ENV NODE_CODE_VERSION=$GIT_COMMIT

CMD ["supercronic", "-passthrough-logs", "/app/node.crontab"]
