#!/usr/bin/env bash
# Register the transactions_scored hybrid table with the Pinot controller:
# one schema shared by a REALTIME table (tails Kafka transactions-scored)
# and an OFFLINE table (nightly Spark-built segments). Idempotent — POSTing
# an existing schema/table updates it.
#
# Run from the host (uses the controller's mapped port 9100):
#   bash utils/pinot/load_tables.sh
set -euo pipefail

CONTROLLER="${PINOT_CONTROLLER:-http://localhost:9100}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Registering schema transactions_scored"
curl -fsS -X POST "${CONTROLLER}/schemas" \
    -H 'Content-Type: application/json' \
    -d @"${HERE}/transactions_scored.schema.json"
echo

echo "==> Registering REALTIME table"
curl -fsS -X POST "${CONTROLLER}/tables" \
    -H 'Content-Type: application/json' \
    -d @"${HERE}/transactions_scored.realtime.tableconfig.json"
echo

echo "==> Registering OFFLINE table"
curl -fsS -X POST "${CONTROLLER}/tables" \
    -H 'Content-Type: application/json' \
    -d @"${HERE}/transactions_scored.offline.tableconfig.json"
echo

echo "==> Tables now registered:"
curl -fsS "${CONTROLLER}/tables"
echo
