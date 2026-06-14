# Step 5 - Customer behavioral baselines in `/analytics`

This step stands up `jobs/features/build_customer_features.py` - a Spark
batch job that reads Step 4's enriched transaction fact, collapses it to
one event per `txn_id`, computes one behavioral baseline row per `card_id`,
and writes `/analytics/customer_features/`.

Step 6 uses these baselines for offline rules and optional ML. Step 7
will broadcast-load the same small Parquet table into Flink so streaming
scoring can compare each new transaction against a card's normal behavior
without re-aggregating history.

This is the sister doc to
[`docs/plans/plan.md` Section Step 5](../plans/plan.md#step-5--customer-behavioral-baselines-feature-store)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `jobs/features/build_customer_features.py` | Spark batch job: enriched fact -> per-card behavioral features |
| `jobs/features/check_customer_features.py` | One-shot verifier for row counts, duplicates, and distributions |
| `/analytics/customer_features/` | Snappy-Parquet, one row per customer card, no partitioning |

The output schema is customer profile columns plus transaction-history
features:

```text
card_id, customer_id, age, income_bracket, account_age_months,
avg_monthly_spend, home_country, typical_categories, credit_limit,
txn_count, avg_amount, stddev_amount, p50_amount, p95_amount, p99_amount,
unique_merchant_count, unique_country_count, unique_category_count,
seen_countries, seen_categories, seen_channels,
pct_intl, pct_online, pct_atm,
first_txn_ts, last_txn_ts, active_days, txns_per_active_day
```

## What this step does NOT do

- **No scoring yet.** These are baselines, not fraud predictions. Step 6
  turns them into rules, scores, and metrics.
- **No label features.** We deliberately do not include `confirmed_fraud`
  counts or rates. Labels are for evaluation, not for streaming-time
  features.
- **No HMS registration.** Output is plain Parquet on HDFS. Step 9
  registers analytics outputs in Hive Metastore for Presto.
- **No partitioning.** This is a small dimension-like feature table
  around 100K rows. Partitioning by `card_id` would create too many tiny
  directories; partitioning by country or income bracket does not help the
  next joins.

## Concepts you'll meet here

- **Aggregations as features.** A fraud rule like "amount is unusual for
  this card" needs a baseline first: average amount, standard deviation,
  high percentiles, usual countries, usual categories, and transaction
  frequency.
- **Approximate aggregations.** `approx_count_distinct` and
  `percentile_approx` trade tiny error for much faster execution. That is
  right for behavioral baselines at this scale. A p95 that is off by a few
  cents is not worth a full exact percentile shuffle.
- **Feature store shape.** Step 7's Flink job should be able to load this
  once and keep it in broadcast state. That means one compact row per card,
  stable column names, and no heavyweight joins at event time.
- **Deduping before aggregation.** The Step 4 enriched table can contain
  duplicate `txn_id`s when the Kafka topic has been replayed or when sparse
  dimensions fan out. A customer's baseline should count transaction
  events, not ingestion duplicates, so this job uses
  `dropDuplicates(["txn_id"])` before grouping by card.

## Pre-flight

```sh
# 1. Step 4 output exists.
docker compose exec namenode hdfs dfs -ls /analytics/transactions_enriched/ | head
# expect: _SUCCESS plus dt=YYYY-MM-DD partition directories.

# 2. Spark can still read HDFS.
make smoke-spark
# expect: green "OK: Spark + HDFS integration works".

# 3. Customer profiles still exist in curated.
docker compose exec namenode hdfs dfs -ls /curated/customer-profiles/
# expect: one or more part-*.snappy.parquet files.

# 4. Step 5 output does not need to exist yet. The job overwrites it.
docker compose exec namenode hdfs dfs -ls /analytics/customer_features/ 2>/dev/null \
    || echo "OK: /analytics/customer_features not yet"
```

If Step 4 is missing, run:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
    /opt/jobs/enrich/build_enriched_fact.py
```

## Warmup - Inspect the rows we aggregate

Before writing the feature job, it helps to see why we dedupe by
`txn_id`. Run a small Spark snippet from the master container:

```sh
docker compose exec spark-master /opt/spark/bin/pyspark \
    --master spark://spark-master:7077
```

Inside the shell:

```python
from pyspark.sql.functions import countDistinct

df = spark.read.parquet("hdfs://namenode:9000/analytics/transactions_enriched/")

print("rows:", df.count())
print("distinct txn_id:", df.select(countDistinct("txn_id")).collect()[0][0])

df.select(
    "txn_id", "timestamp", "card_id", "amount", "country",
    "merchant_category", "channel", "is_international"
).show(5, truncate=False)
```

If `rows` is larger than `distinct txn_id`, that is expected in this local
project after repeated producer replays. Step 5 uses the distinct event
count as the feature base.

Exit with `Ctrl+D`.

## 5a - Build the feature job

Create `jobs/features/build_customer_features.py`.

The job has four pieces:

1. Read `/analytics/transactions_enriched/`.
2. Select only transaction-history columns and `dropDuplicates(["txn_id"])`.
3. Group by `card_id` and compute behavioral features.
4. Join the static customer profile fields and write
   `/analytics/customer_features/`.

Submit:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/features/build_customer_features.py
```

No `--packages` flag is needed. This job reads Parquet from HDFS, not
Kafka.

Expected output:

```text
transaction events used: 1000000
wrote 100000 card feature rows to hdfs://namenode:9000/analytics/customer_features/
```

The exact event count follows the source data. If the Kafka topic has
multiple replays, the enriched row count can be higher, but the distinct
`txn_id` count should still be 1M for the generated dataset.

## 5b - Add the verifier

Create `jobs/features/check_customer_features.py`.

This verifier reads `/analytics/customer_features/` and prints:

- total rows and distinct cards,
- max and average `txn_count`,
- average `avg_amount`, `pct_intl`, and `pct_online`,
- any duplicate `card_id` rows,
- `describe()` output for the main numeric features,
- a small row sample.

Run:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/features/check_customer_features.py
```

Expected:

```text
rows = 100000
distinct_cards = 100000
duplicate card_id rows: no output
avg_txn_count around 10
avg_of_avg_amount in a plausible transaction range
```

## Verification

Run all checks:

```sh
# 1. Feature path exists.
docker compose exec namenode hdfs dfs -ls /analytics/customer_features/
# expect: _SUCCESS plus a few part-*.snappy.parquet files.

# 2. Verifier passes the row-count and duplicate-card checks.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/features/check_customer_features.py

# 3. Feature table is one row per card.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/features/check_customer_features.py \
    | grep -E "rows|distinct_cards|Duplicate"
```

The third command is only a skim helper. The real signal is the verifier's
first table: `rows` and `distinct_cards` should match.

## What "passes" looks like

```text
Feature store row checks:
+------+--------------+-------------+-------------+------------------+------------+--------------+
|rows  |distinct_cards|max_txn_count |avg_txn_count|avg_of_avg_amount |avg_pct_intl|avg_pct_online|
+------+--------------+-------------+-------------+------------------+------------+--------------+
|100000|100000        |...          |10.0         |...               |...         |...           |
+------+--------------+-------------+-------------+------------------+------------+--------------+

Duplicate card_id rows; expect no output:
+-------+-----+
|card_id|count|
+-------+-----+
+-------+-----+
```

## Troubleshooting

- **`Path does not exist: /analytics/transactions_enriched/`.** Step 4 has
  not been run in the current HDFS volume. Run
  `jobs/enrich/build_enriched_fact.py` first.
- **`rows != distinct_cards` in the verifier.** The output is no longer one
  row per card. Check that `attach_customer_profile()` joins on `card_id`
  and that `customer-profiles` itself has one row per card.
- **`avg_txn_count` is around 30 or 40 instead of around 10.** The job is
  probably aggregating duplicate Step 4 rows. Confirm
  `dropDuplicates(["txn_id"])` is still in `read_transaction_events()`.
- **`pct_online` is all zero.** Check the `channel` values in the enriched
  fact. The generator uses lowercase strings like `online`, `in_store`,
  `contactless`, and `atm`; the comparison should use `"online"`.
- **Many null feature columns.** That means some customer cards have no
  transaction history in the enriched fact. That can happen in a tiny test
  replay. The job fills scalar numeric features with zero, but arrays and
  timestamps stay null because there is no historical behavior to summarize.

## What's next

Step 6 reads `/analytics/transactions_enriched/` and
`/analytics/customer_features/`, computes fraud rules such as high amount,
velocity, unusual geography, VPN/unknown device, and high-risk merchant,
then evaluates the rules against `confirmed_fraud` with imbalance-aware
metrics.
