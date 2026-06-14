# Step 6 - Offline fraud detection scoring

This step stands up `jobs/score/score_offline.py` - a Spark batch job
that reads the enriched transaction fact and customer feature store,
applies five fraud rules, writes `/analytics/scored/`, and prints
imbalance-aware evaluation metrics against `confirmed_fraud`.

Step 7 will reuse the same rule logic in Flink for real-time scoring.
Step 6 proves the rules work offline before we move them into a
long-lived streaming job.

This is the sister doc to
[`docs/plans/plan.md` Section Step 6](../plans/plan.md#step-6--offline-detection-rules--a-simple-ml-baseline)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `jobs/score/score_offline.py` | Spark batch job: rules + risk score + predictions |
| `jobs/score/check_offline_scores.py` | One-shot verifier for metrics and rule trigger rates |
| `/analytics/scored/` | Snappy-Parquet, one row per transaction, partitioned by `dt` |

The output schema keeps the transaction keys, the five rule flags, the
aggregate score, and the label for evaluation:

```text
txn_id, timestamp, card_id, merchant_id, amount, country, channel,
is_international, merchant_risk_score, confirmed_fraud, dt,
rule_high_amount, rule_velocity, rule_intl_mismatch,
rule_unknown_device_vpn, rule_high_risk_merchant,
risk_score, predicted_fraud, recommended_action
```

## What this step does NOT do

- **No ML model yet.** This step ships interpretable rules first. A
  simple Spark MLlib classifier is an optional follow-up, not required
  to unblock Step 7.
- **No streaming.** Velocity here is computed with a Spark window over
  the full offline dataset. Flink will re-implement velocity with true
  event-time state in Step 7.
- **No HMS registration.** Output is plain Parquet on HDFS. Step 9
  registers analytics tables for Presto.
- **No Pinot writes.** Pinot consumes the Flink output topic in Step 8,
  not this offline scored table.

## Concepts you'll meet here

- **Rule-based detection first.** Fraud teams usually ship explicit
  rules before ML because rules are explainable and fast to tune.
- **Class imbalance.** Only about 1.3% of transactions are confirmed
  fraud in the generated seed data. Accuracy is misleading; use
  precision, recall, F1, and business impact instead.
- **Label noise.** `confirmed_fraud` comes from sparse fraud reports
  and includes false alarms. A perfect classifier cannot reach 100%
  recall on this label.
- **Windowed velocity offline.** The velocity rule counts how many
  transactions a card has in the previous 10 minutes using
  `Window.partitionBy("card_id").orderBy(unix_timestamp("timestamp"))
  .rangeBetween(-600, 0)`. This is the offline analogue of the Flink
  keyed window in Step 7.
- **Deduping before scoring.** Like Step 5, we collapse duplicate
  `txn_id`s from Kafka replays or sparse-dimension fan-out before
  applying rules.

## Pre-flight

```sh
# 1. Step 4 output exists.
docker compose exec namenode hdfs dfs -ls /analytics/transactions_enriched/ | head

# 2. Step 5 output exists.
docker compose exec namenode hdfs dfs -ls /analytics/customer_features/

# 3. Spark can still read HDFS.
make smoke-spark

# 4. Step 6 output does not need to exist yet.
docker compose exec namenode hdfs dfs -ls /analytics/scored/ 2>/dev/null \
    || echo "OK: /analytics/scored not yet"
```

If Step 4 or Step 5 is missing, rerun those jobs first.

## Warmup - Inspect one card's velocity pattern

Before writing the scoring job, open PySpark and look at one card with
several transactions close together:

```sh
docker compose exec spark-master /opt/spark/bin/pyspark \
    --master spark://spark-master:7077
```

Inside the shell:

```python
from pyspark.sql.functions import col, count, unix_timestamp
from pyspark.sql.window import Window

df = (spark.read.parquet("hdfs://namenode:9000/analytics/transactions_enriched/")
      .dropDuplicates(["txn_id"])
      .filter(col("card_id") == "CARD-000001")
      .orderBy("timestamp"))

w = Window.partitionBy("card_id").orderBy(unix_timestamp("timestamp")).rangeBetween(-600, 0)
(
    df.withColumn("velocity_count", count("*").over(w))
      .select("txn_id", "timestamp", "amount", "velocity_count")
      .show(20, truncate=False)
)
```

You should see `velocity_count` climb when several transactions land
within 10 minutes of each other.

Exit with `Ctrl+D`.

## 6a - Build the offline scoring job

Create `jobs/score/score_offline.py`.

The five rules from the brief:

| Rule | Condition |
|---|---|
| `rule_high_amount` | `amount > 3 * avg_monthly_spend / 30` |
| `rule_velocity` | 5+ transactions from the same card in 10 minutes |
| `rule_intl_mismatch` | international txn from a card that has only ever used `home_country` |
| `rule_unknown_device_vpn` | unknown device and VPN flagged |
| `rule_high_risk_merchant` | `merchant_risk_score >= 8` |

Aggregate scoring:

- `risk_score` = sum of the five rule flags (0-5)
- `predicted_fraud` = `risk_score >= 2`
- `recommended_action` = `approve` / `review` / `block`

Submit:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/score/score_offline.py
```

Expected:

```text
wrote 1000000 scored rows to hdfs://namenode:9000/analytics/scored/
precision=...
recall=...
f1=...
prevented_loss_estimate=...
```

No `--packages` flag is needed.

## 6b - Add the verifier

Create `jobs/score/check_offline_scores.py`.

Run:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/score/check_offline_scores.py
```

Expected:

- `rows = 1000000`
- confusion matrix with non-zero TP and FP
- each rule trigger rate printed
- a small sample of predicted-fraud rows

## Verification

```sh
# 1. Scored output exists and is partitioned by dt.
docker compose exec namenode hdfs dfs -ls /analytics/scored/ | head

# 2. Verifier prints metrics and rule rates.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/score/check_offline_scores.py

# 3. Row count matches distinct txn_id from Step 4.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/score/score_offline.py 2>/dev/null | grep "wrote"
```

## What "passes" looks like

On the generated seed data with `risk_score >= 2`:

```text
wrote 1000000 scored rows to hdfs://namenode:9000/analytics/scored/

Confusion matrix:
  TP=1130  FP=36785
  FN=11541  TN=950544

Metrics:
  precision=0.0298
  recall=0.0892
  f1=0.0447

Rule trigger rates:
  rule_high_amount=0.535
  rule_velocity=0.000
  rule_intl_mismatch=0.000
  rule_unknown_device_vpn=0.005
  rule_high_risk_merchant=0.066
```

This is expected on the first pass. `rule_high_amount` is aggressive
(`3x monthly spend / 30`), so it fires on many legitimate transactions.
`rule_velocity` stays at zero because the synthetic dataset has no
bursts of 5+ transactions within 10 minutes (max observed velocity is 2).
`rule_intl_mismatch` stays at zero because most cards already transact in
multiple countries. The important Step 6 outcome is that the job runs,
writes `/analytics/scored/`, and prints imbalance-aware metrics you can
tune in the notebook or before Step 7.

## Troubleshooting

- **`Path does not exist: /analytics/customer_features/`.** Run Step 5
  first.
- **`wrote` count is much larger than 1M.** Duplicate `txn_id`s are
  getting through. Confirm `dropDuplicates(["txn_id"])` is still in
  `read_scoring_inputs()`.
- **Recall is 0.** One or more joins failed silently. Check that
  `avg_monthly_spend`, `home_country`, and `seen_countries` are present
  after the feature join.
- **Precision is extremely low.** Lower the threshold only after
  inspecting which rules fire most often. Start by printing rule trigger
  rates from `check_offline_scores.py`.
- **Velocity rule never fires.** Confirm the window uses
  `unix_timestamp("timestamp")` and `rangeBetween(-600, 0)`.

## What's next

Step 7 ports the same rule set into a Flink streaming job that reads
Kafka `transactions`, broadcast-loads `/analytics/customer_features/`,
writes `transactions-scored` and `fraud-alerts`, and handles true
event-time velocity with watermarks and exactly-once Kafka sinks.
