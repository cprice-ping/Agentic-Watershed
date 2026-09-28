#!/bin/sh
# Run one node script the way cron on the Pi did: from the script's own
# directory, because every stack resolves data/ and its sibling modules
# relative to where it runs.
#
#   run-job.sh /app/River/collector.py [args...]
#
# Output goes to the container's stdout, which docker keeps (size-capped in
# docker-compose.yml) and `docker compose logs node` reads. There are no
# per-stack log files any more; nothing in the repo read them.
set -eu
script="$1"
shift
echo "run-job: $script (code ${NODE_CODE_VERSION:-unknown})"
cd "$(dirname "$script")"
exec python "$script" "$@"
