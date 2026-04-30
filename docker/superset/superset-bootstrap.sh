#!/usr/bin/env bash
# Long-running entrypoint for the Superset web container.
# Installs pinned extras (pinotdb etc.) into the running container's
# Python env, then hands off to the image's stock run-server.sh.
#
# Pip-install runs every container start because site-packages live in
# the image filesystem (NOT a named volume), so the layer is fresh on
# every restart. Trading ~15s of startup cost for not having to bake a
# custom Dockerfile.
set -euo pipefail

REQS=/app/docker/requirements-local.txt
if [ -f "$REQS" ]; then
  echo "[superset-bootstrap] pip install -r $REQS"
  pip install --no-cache-dir -r "$REQS"
fi

echo "[superset-bootstrap] exec run-server.sh"
exec /usr/bin/run-server.sh
