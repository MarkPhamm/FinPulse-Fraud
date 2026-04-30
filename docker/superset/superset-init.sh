#!/usr/bin/env bash
# One-shot init for Superset: pip-install extras, run db migrations,
# create the admin user (idempotent), and load default roles + perms.
#
# Bind-mounted into the apache/superset:4.1.1 image because that image
# doesn't ship the official docker-init.sh used by Superset's compose
# example. Path inside container: /app/superset-init.sh
set -euo pipefail

REQS=/app/docker/requirements-local.txt
if [ -f "$REQS" ]; then
  echo "[superset-init] pip install -r $REQS"
  pip install --no-cache-dir -r "$REQS"
fi

echo "[superset-init] superset db upgrade"
superset db upgrade

# create-admin is idempotent: if the user exists it just no-ops with a warning.
echo "[superset-init] create admin user (${ADMIN_USERNAME})"
superset fab create-admin \
  --username "${ADMIN_USERNAME}" \
  --firstname "${ADMIN_FIRSTNAME}" \
  --lastname "${ADMIN_LASTNAME}" \
  --email "${ADMIN_EMAIL}" \
  --password "${ADMIN_PASSWORD}" || true

echo "[superset-init] superset init (roles + permissions)"
superset init

echo "[superset-init] done"
