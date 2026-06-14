# Step 7 - Flink real-time scoring (Kafka to Kafka)

This step stands up a long-lived **Flink SQL** job that consumes Kafka
topic `transactions`, computes an event-time velocity count, enriches each
event with the customer baseline and merchant risk (both published to
compacted Kafka topics), applies the streaming fraud rules, and writes
every scored event to `transactions-scored` plus high-risk events to
`fraud-alerts`.

`transactions-scored` feeds Pinot's real-time table in Step 8;
`fraud-alerts` feeds the Superset alert ticker in Step 10.

This is the sister doc to
[`docs/plans/plan.md` Section Step 7](../plans/plan.md#step-7--flink-real-time-scoring-kafka--flink--kafka)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `make flink-deps` + compose mounts | Fetch the Flink SQL Kafka connector jar (not bundled) and overlay it into `/opt/flink/lib` |
| `jobs/features/publish_customer_features.py` | Spark job: publish `/analytics/customer_features/` to compacted topic `customer-features` |
| `jobs/features/publish_merchant_directory.py` | Spark job: publish merchant risk to compacted topic `merchant-directory` |
| `src/consumer/stream_score.sql` | Flink SQL job: source, velocity window, dim joins, rules, dual sink |
| topics `transactions-scored`, `fraud-alerts` | Scored stream + alert stream |
| topics `customer-features`, `merchant-directory` | Compacted dimension streams the scorer joins against |

## Infrastructure this step had to fix

The base `flink:1.19.1` image is missing two things the plan assumed:

1. **No Kafka connector.** `/opt/flink/lib` ships only the files
   connector. `make flink-deps` downloads
   `flink-sql-connector-kafka-3.2.0-1.19.jar` into `docker/flink/lib/`
   (gitignored, same posture as `make hive-deps`), and compose overlays
   it into `/opt/flink/lib/flink-sql-connector-kafka.jar` on both Flink
   containers.
2. **Checkpoint directory ownership.** The Flink JVM runs as the
   unprivileged `flink` user (uid 9999), but the `flink-data` named
   volume mounts as `root:root`, so the checkpoint coordinator cannot
   create `/opt/flink/data/checkpoints/<job>/shared`. Both Flink services
   now start as root with an entrypoint that `chown`s the volume to
   `flink` and then hands off to the stock entrypoint (which drops back
   to `flink`). This survives `make nuke`.

## What this step does NOT do

- **No Spark.** Spark is batch-only in this stack. Flink owns streaming.
- **No HDFS reads inside Flink.** Rather than read Parquet from HDFS in
  Flink, the small dimensions are published to compacted Kafka topics and
  consumed as upsert-kafka versioned tables. This sidesteps the Hadoop
  classpath and keeps the job pure Kafka in / Kafka out.
- **Not all five offline rules.** The streaming scorer fires the four
  rules computable from the transaction stream plus the customer and
  merchant dimensions: `high_amount`, `velocity`, `intl_mismatch`,
  `high_risk_merchant`. The device/VPN rule needs the device-fingerprint
  dimension and is reconciled offline (Step 6). This is the
  real-time-partial / offline-complete pattern.

## Concepts you'll meet here

- **Event time vs processing time.** The velocity window operates on the
  transaction's own `event_time` (parsed from the JSON `timestamp`), not
  on arrival time, with a 2-minute out-of-orderness watermark.
- **OVER-window velocity.** `COUNT(*) OVER (PARTITION BY card_id ORDER BY
  event_time RANGE BETWEEN INTERVAL '10' MINUTE PRECEDING AND CURRENT
  ROW)` is the streaming analogue of the Step 6 Spark window. OVER windows
  preserve the event-time attribute and emit append-only rows.
- **upsert-kafka as a versioned lookup.** A compacted topic keyed by the
  dimension PK, consumed with the `upsert-kafka` connector, materializes
  the latest value per key in Flink state - a lightweight stand-in for
  broadcast state at this scale. `value.fields-include = 'EXCEPT_KEY'`
  means the key column is not repeated in the JSON value.
- **Null-safe rules.** Every rule is wrapped in `COALESCE(..., FALSE)` so
  a transaction whose dimension row has not loaded into join state yet
  scores a definite 0 rather than SQL NULL (which would null out the
  whole `risk_score` via null arithmetic).
- **STATEMENT SET = one job, two sinks.** Wrapping both `INSERT INTO`s in
  `EXECUTE STATEMENT SET BEGIN ... END;` runs them as a single job that
  shares one checkpoint, so `transactions-scored` and `fraud-alerts`
  never disagree about whether a txn was processed.
- **Exactly-once, effectively.** Both sinks are `upsert-kafka` keyed by
  `txn_id`. Combined with 30s checkpoints, a replay after restart
  re-emits the same key and the latest value wins - idempotent output.

## Pre-flight

```sh
# 1. The connector jar is present (make up / make up-stream fetch it).
make flink-deps
ls docker/flink/lib/

# 2. Flink is healthy with a registered taskmanager.
make smoke-flink

# 3. Step 5 output exists (customer features) and merchant dim is curated.
docker compose exec namenode hdfs dfs -ls /analytics/customer_features/
docker compose exec namenode hdfs dfs -ls /curated/merchant-directory/

# 4. The transactions topic has data (Step 3).
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic transactions
```

## 7a - Create the output and dimension topics

```sh
for t in transactions-scored fraud-alerts; do
  docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9094 \
    --create --if-not-exists --topic "$t" --partitions 6 --replication-factor 1 \
    --config retention.ms=-1
done
for t in customer-features merchant-directory; do
  docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9094 \
    --create --if-not-exists --topic "$t" --partitions 6 --replication-factor 1 \
    --config cleanup.policy=compact
done
```

The dimension topics are **compacted** (keep the latest value per key);
the scored/alert topics use **infinite retention** like `transactions`.

## 7b - Publish the dimensions to Kafka

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
    /opt/jobs/features/publish_customer_features.py
# expect: published 100000 card feature rows to topic 'customer-features'

docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
    /opt/jobs/features/publish_merchant_directory.py
# expect: published 10000 merchant rows to topic 'merchant-directory'
```

Re-run these whenever the nightly batch rebuilds the feature store - the
compacted topics keep only the latest value per key.

## 7c - Submit the streaming job

```sh
docker compose exec -T flink-jobmanager \
    /opt/flink/bin/sql-client.sh -f /opt/flink/usrlib/stream_score.sql
# expect: a "Job ID: ..." line; the SQL client exits after submitting.
```

Confirm it is running:

```sh
docker compose exec flink-jobmanager /opt/flink/bin/flink list
# expect: finpulse-stream-score (RUNNING)
```

The job reads `transactions` from `earliest-offset`, so it immediately
reprocesses whatever is already in the topic and then tails new events.

## 7d - Verify both output streams

```sh
# Scored stream fills up (one record per processed txn).
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic transactions-scored

# Alerts fill up (risk_score >= 2 only).
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic fraud-alerts

# Sample an alert.
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server kafka:9094 --topic fraud-alerts \
    --from-beginning --max-messages 3
```

## What "passes" looks like

A scored record carries the full 4-rule schema:

```json
{"txn_id":"TXN-0039165","card_id":"CARD-012583","event_time":"2025-03-04 12:44:35",
 "merchant_id":"MERCH-05801","amount":218.68,"country":"US","channel":"online",
 "is_international":false,"velocity_count":1,"merchant_risk_score":1,
 "rule_high_amount":false,"rule_velocity":false,"rule_intl_mismatch":false,
 "rule_high_risk_merchant":false,"risk_score":0,"predicted_fraud":false,
 "recommended_action":"approve"}
```

An alert record:

```json
{"txn_id":"TXN-0009425","card_id":"CARD-015174","event_time":"2025-01-02 02:31:44",
 "amount":230.13,"risk_score":2,"triggered_rules":"high_amount,high_risk_merchant",
 "recommended_action":"review"}
```

On the generated seed data, alerts are driven by `high_amount` +
`high_risk_merchant`. `velocity` never fires (the data has no card with
5+ transactions in any 10-minute event-time window) and `intl_mismatch`
is rare (most cards already transact in multiple countries) - the same
shape seen offline in Step 6.

## Managing the job

```sh
# List / cancel.
docker compose exec flink-jobmanager /opt/flink/bin/flink list
docker compose exec flink-jobmanager /opt/flink/bin/flink cancel <jobid>

# Savepoint + resume (exactly-once across an upgrade).
docker compose exec flink-jobmanager /opt/flink/bin/flink stop --savepoint /opt/flink/data/savepoints <jobid>
docker compose exec flink-jobmanager /opt/flink/bin/sql-client.sh \
    -Dexecution.savepoint.path=<savepoint-path> -f /opt/flink/usrlib/stream_score.sql
```

## Troubleshooting

- **`Could not find any factory for identifier 'kafka'`.** The connector
  jar is missing from `/opt/flink/lib`. Run `make flink-deps` and recreate
  the Flink containers (`docker compose up -d flink-jobmanager
  flink-taskmanager`).
- **`Failed to create directory for shared state`.** The `flink-data`
  volume is root-owned and the Flink user cannot write checkpoints. The
  root entrypoint in compose chowns it on start; recreate the containers
  if you see this after a manual volume change.
- **`risk_score` is null in scored records.** A rule returned SQL NULL
  because a dimension row was not yet in join state. Every rule must be
  `COALESCE(..., FALSE)`.
- **No alerts on `fraud-alerts`.** Confirm both dimension topics were
  published (7b) and that the job is the 4-rule version (it joins
  `merchant_features_src`). With only customer features, the max realistic
  score is 1 and nothing reaches the alert threshold of 2.
- **Job stuck at CREATED / no slots.** The taskmanager did not register;
  check `taskmanager.host` resolves and `make smoke-flink` passes.

## What's next

Step 8 stands up the Pinot hybrid table `transactions_scored`: a real-time
table tailing the `transactions-scored` topic this job produces, unioned
with an offline table rebuilt nightly by Spark from
`/analytics/transactions_enriched/`.
