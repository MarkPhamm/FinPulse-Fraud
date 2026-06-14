# Step 8 - Pinot hybrid table (real-time + offline)

This step stands up the Pinot **hybrid table** `transactions_scored`: one
logical table backed by a REALTIME table that tails the Kafka topic
`transactions-scored` (produced by the Step 7 Flink job) and an OFFLINE
table of segments rebuilt from the Step 6 scored Parquet. The Pinot broker
unions the two at query time using a time boundary, so dashboards get
sub-second latency on fresh events and audit-grade history.

This is the sister doc to
[`docs/plans/plan.md` Section Step 8](../plans/plan.md#step-8--pinot-hybrid-table-real-time-from-kafka--offline-from-hdfs)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `utils/pinot/transactions_scored.schema.json` | One schema shared by both physical tables |
| `utils/pinot/transactions_scored.realtime.tableconfig.json` | REALTIME table consuming Kafka `transactions-scored` |
| `utils/pinot/transactions_scored.offline.tableconfig.json` | OFFLINE table for nightly Spark-built segments |
| `utils/pinot/load_tables.sh` | Registers the schema + both table configs |
| `jobs/score/export_pinot_offline.py` | Spark job: `/analytics/scored/` to local Parquet for ingestion |
| `utils/pinot/offline_ingestion_job.yaml` | Pinot standalone batch ingestion spec |
| `./pinot-offline` shared mount | Spark-to-Pinot Parquet handoff (no HDFS plugin needed) |

## Infrastructure this step had to add

The base `apachepinot/pinot:1.2.0` image ships
`pinot-batch-ingestion-standalone` and a `pinot-parquet` reader, but **no
HDFS filesystem plugin**, so Pinot cannot read `/analytics/scored` directly
from HDFS. The workaround: Spark writes the scored rows as Parquet to the
`./pinot-offline` host directory, which is bind-mounted into the Spark
master + both workers (read-write) and the Pinot controller (read-only).
Pinot's standalone ingestion then reads `file:///opt/pinot-offline/scored`.

## What this step does NOT do

- **No upsert table.** `transactions_scored` is append-only. The Flink
  sinks key by `txn_id` for idempotency, but Pinot is not configured as an
  upsert table here - the static replay produces no key updates.
- **No deep-store on HDFS.** Segments live on the controller's local
  `pinot-controller-data` volume. Production would point the deep store at
  HDFS so any server can rehydrate any segment.
- **No star-tree index.** Inverted + range indexes are configured; a
  star-tree for a specific aggregate is a follow-up optimization.

## Concepts you'll meet here

- **Hybrid table = two physical tables, one schema, one name.** Register a
  `transactions_scored_REALTIME` and a `transactions_scored_OFFLINE` that
  share the schema name `transactions_scored`. The broker exposes the
  single logical name and routes each time slice to the right half.
- **Time boundary / cutover.** The broker serves `event_time` before the
  offline table's max time from OFFLINE segments and after it from the
  REALTIME table, so the two never double-count. (This is why a hybrid
  `count(*)` can differ slightly from the offline-only count.)
- **Real-time ingestion.** Pinot servers tail Kafka directly with a
  low-level consumer + JSON decoder - no Spark or Flink in the read path.
  A consuming segment builds in memory, then seals to a columnar segment
  on a row/time threshold.
- **Offline ingestion.** `SegmentCreationAndTarPush` builds immutable
  segments from Parquet and pushes the tars to the controller. The nightly
  Airflow DAG (Step 11) runs this after offline scoring to roll yesterday
  into the offline half.

## Pre-flight

```sh
# 1. Pinot is healthy.
make smoke-pinot

# 2. The Flink job is producing transactions-scored (Step 7).
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic transactions-scored

# 3. The scored Parquet exists (Step 6).
docker compose exec namenode hdfs dfs -ls /analytics/scored/ | head

# 4. The shared mount exists on Spark + the Pinot controller.
docker compose exec pinot-controller ls -ld /opt/pinot-offline
```

## 8a - Register the hybrid table

```sh
bash utils/pinot/load_tables.sh
# expect: schema + REALTIME + OFFLINE "successfully added", then
#         {"tables":["transactions_scored"]}
```

The REALTIME table starts consuming `transactions-scored` from `smallest`
immediately, so it backfills whatever the Flink job has already produced.

## 8b - Verify the real-time path

```sh
curl -fsS -X POST http://localhost:8099/query/sql \
    -H 'Content-Type: application/json' \
    -d '{"sql":"SELECT count(*) FROM transactions_scored"}'

curl -fsS -X POST http://localhost:8099/query/sql \
    -H 'Content-Type: application/json' \
    -d '{"sql":"SELECT recommended_action, count(*) FROM transactions_scored GROUP BY recommended_action"}'
```

Expect a non-zero count climbing toward the `transactions-scored` topic
size, and a `review` bucket matching the alert volume from Step 7.

## 8c - Build and push the offline segments

```sh
# 1. Spark exports scored rows to the shared Parquet dir.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/score/export_pinot_offline.py
# expect: exported 1000000 scored rows to file:///opt/pinot-offline/scored

# 2. Make the ingestion spec visible to the controller, then run it.
cp utils/pinot/offline_ingestion_job.yaml pinot-offline/
docker compose exec pinot-controller bin/pinot-admin.sh LaunchDataIngestionJob \
    -jobSpecFile /opt/pinot-offline/offline_ingestion_job.yaml
```

`export_pinot_offline.py` accepts `--date YYYY-MM-DD` to export a single
partition - that is the per-day mode the nightly Airflow DAG uses.

## 8d - Verify the hybrid table

```sh
# Offline segment registered.
curl -fsS http://localhost:9100/segments/transactions_scored_OFFLINE

# Offline-only vs hybrid counts (hybrid applies the cutover, so it can be
# slightly lower than offline-only - that is correct, not data loss).
curl -fsS -X POST http://localhost:8099/query/sql \
    -d '{"sql":"SELECT count(*) FROM transactions_scored_OFFLINE"}'
curl -fsS -X POST http://localhost:8099/query/sql \
    -d '{"sql":"SELECT count(*) FROM transactions_scored"}'
```

## What "passes" looks like

```text
$ curl .../segments/transactions_scored_OFFLINE
[{"OFFLINE":["transactions_scored_offline_2025-01-01_2025-06-29_0"]}]

$ SELECT count(*) FROM transactions_scored_OFFLINE   -> 1000000
$ SELECT count(*) FROM transactions_scored           -> ~994000   (hybrid cutover)
$ SELECT predicted_fraud, count(*) ... GROUP BY ...   -> true ~6700, false ~1.02M
```

## Troubleshooting

- **`no segments found` / count is 0.** The broker/server did not register,
  or the table type is wrong. Check `make smoke-pinot` and the controller
  UI's *Tables* view.
- **Real-time count stuck at 0.** The decoder/consumer-factory class names
  must be `KafkaJSONMessageDecoder` and `kafka20.KafkaConsumerFactory`, and
  `stream.kafka.broker.list` must be the in-network `kafka:9094`.
- **Offline push fails with `pushSpec is null`.** The ingestion job spec
  needs a `pushJobSpec` block - it is included in
  `offline_ingestion_job.yaml`.
- **Ingestion cannot find input.** Confirm the Spark export wrote into
  `./pinot-offline/scored` on the host and that the controller mounts it at
  `/opt/pinot-offline` (the spec reads `file:///opt/pinot-offline/scored`).
- **Type mismatch / null columns.** Pinot coerces silently. Validate the
  Flink JSON and the exported Parquet against
  `transactions_scored.schema.json`.

## What's next

Step 9 registers the granular `/curated/*` and `/analytics/*` Parquet in
the Hive Metastore so PrestoDB can serve row-level ad-hoc SQL - the
complementary access pattern to Pinot's pre-aggregated serving.
