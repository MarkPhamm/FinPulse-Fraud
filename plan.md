# FinPulse-Fraud — End-to-End Implementation Plan

## Context

The infrastructure (HDFS + Spark + Kafka + Airflow on Docker) is up and the
smoke job passes. The 5 source datasets (1M transactions, 100K customers,
600K device sessions, 15K fraud reports, 10K merchants) are sitting in
`data/` as gzipped CSV/JSON.

The class brief in [`docs/scenario.md`](docs/scenario.md) defines four stages
(HDFS Lake → Spark Batch → Kafka Streaming → Airflow Orchestration) and
seven business questions to answer. The rubric weights **feature
engineering depth, class-imbalance awareness, real-time architecture
quality, and dollar-impact framing** — not pure accuracy.

The goal of this plan is to turn that brief into 10 small, observable
steps so each new concept (HDFS zones, Parquet partitioning, broadcast
joins, watermarks, Kafka keys, Airflow sensors, etc.) lands one at a time.
Every step ends with something runnable and a single command to verify it.

**Conventions used throughout the steps:**

- All HDFS paths use the cluster-internal scheme `hdfs://namenode:9000/...`.
  Three top-level zones: `/landing` (raw, immutable), `/curated` (Parquet,
  cleaned), `/analytics` (joined / feature-engineered, query-optimised).
- All Spark jobs run as `spark-submit --master spark://spark-master:7077`
  via `docker compose exec spark-master ...`. New jobs land in `jobs/`
  next to the existing `smoke_spark.py`.
- Each step writes its outputs at known paths so the next step can read
  them; if you re-run the whole pipeline from scratch, just delete the
  `/curated` and `/analytics` dirs first.

---

## At a glance

| Step | Output                                                                | New concept                                |
| ---- | --------------------------------------------------------------------- | ------------------------------------------ |
| 0    | infra healthy                                                         | (done)                                     |
| 1    | `/landing/*` raw `.gz` on HDFS                                        | zones, replication factors                 |
| 2    | `/curated/transactions/` Parquet by date                              | columnar format, partitioning              |
| 3    | `/curated/{devices,customers,merchants,fraud-reports}/`               | JSON `multiLine`, dim vs fact              |
| 4    | `/analytics/transactions_enriched/`                                   | broadcast joins, label leakage             |
| 5    | `/analytics/customer_features/`                                       | aggregations as features, approx aggs      |
| 6    | rule-flagged + (optional) ML scored                                   | class imbalance, PR-AUC                    |
| 7    | Kafka producer replaying CSV → `transactions`                         | partition keys, replay modes               |
| 8    | Streaming `stream_score.py` → `fraud-alerts` / `fraud-scores`         | watermarks, state, `foreachBatch`          |
| 9    | `daily_batch` + `streaming_monitor` DAGs                              | quality gates, BashOperator pattern        |
| 10   | `notebooks/analysis.ipynb` answering the 7 business questions         | $-impact framing                           |

---

## Step 0 — Infrastructure (DONE)

What exists today: `make up` brings the full stack to healthy in ~90s;
`make smoke` exercises HDFS, Kafka, and Spark end-to-end;
`make smoke-airflow` triggers a trivial DAG and waits for success.

**Verify before starting Step 1:**

```sh
make ps              # all services should be Up / healthy
make smoke           # HDFS round-trip + Kafka + Spark smoke job
```

If anything is red, fix it first — every later step assumes this baseline.

---

## Step 1 — Land the raw datasets into HDFS

**Goal.** Get all five `.gz` files from `data/` into `/landing/<dataset>/`
on HDFS, **untouched**. The landing zone is the immutable record of what
the upstream provider gave us — no transforms, no schema changes.

**Concepts you'll meet for the first time.**

- HDFS zone pattern (landing / curated / analytics) and why immutability
  matters for audit + reproducibility.
- Per-directory replication factors. The brief asks for higher replication
  on `fraud-reports` (audit) and `customer-profiles` (regulatory).
- The `hdfs dfs -put` and `hdfs dfs -setrep` commands.

**What to build.**

- `jobs/land_data.py` — a small PySpark *or* plain Python+`hdfs` CLI
  driver that, idempotently, creates the zone dirs and uploads each
  source file. Plain `bash` via the namenode container is also fine for
  this step — keep it minimal.
- Suggested layout:

  ```
  /landing/transactions/transactions.csv.gz
  /landing/customer-profiles/customer-profiles.json.gz
  /landing/merchant-directory/merchant-directory.csv.gz
  /landing/device-fingerprints/device-fingerprints.csv.gz
  /landing/fraud-reports/fraud-reports.json.gz
  ```

- After upload, set replication: `hdfs dfs -setrep 3 /landing/fraud-reports`
  and `/landing/customer-profiles`. Default is 2 (set in `hdfs-site.xml`).

**Verify.**

```sh
docker compose exec namenode hdfs dfs -ls -R /landing
docker compose exec namenode hdfs dfs -stat "%r %n" /landing/fraud-reports/*
# %r = replication; should print 3 for the regulatory datasets
```

---

## Step 2 — Curate transactions to partitioned Parquet

**Goal.** Read `/landing/transactions/transactions.csv.gz`, write it to
`/curated/transactions/` as Parquet, partitioned by `dt` (date derived
from `timestamp`). 1M rows → ~180 day-partitions.

**Concepts.**

- Why Parquet beats CSV for analytics: columnar layout, predicate +
  projection pushdown, splittable, ~5–10× smaller than gzipped CSV.
- Partitioning vs bucketing — when each makes sense. Date is the right
  partition column here because every downstream query filters on a time
  window.
- The CSV gzip splittability gotcha: gz is read by a single task, so
  `repartition(N)` *before* `.write` if you want parallel writes.

**What to build.**

- `jobs/curate_transactions.py`:
  - `spark.read.option("header", "true").option("inferSchema", "false").csv("hdfs://namenode:9000/landing/transactions/transactions.csv.gz")`
  - Cast types explicitly (don't trust `inferSchema` — it triggers a
    full extra pass on a single task because gz isn't splittable).
  - `withColumn("dt", to_date("timestamp"))`
  - `.repartition("dt").write.mode("overwrite").partitionBy("dt").parquet(...)`

**Verify.**

```sh
docker compose exec namenode hdfs dfs -ls /curated/transactions/ | head
# expect ~180 dt=YYYY-MM-DD/ subdirs

docker compose exec spark-master /opt/spark/bin/spark-submit --master spark://spark-master:7077 \
  -c "from pyspark.sql import SparkSession; \
      s = SparkSession.builder.getOrCreate(); \
      df = s.read.parquet('hdfs://namenode:9000/curated/transactions/'); \
      df.printSchema(); print('rows:', df.count())"
# expect 1,000,000 rows
```

---

## Step 3 — Curate the four remaining datasets

**Goal.** Same Parquet conversion for the other four, each with
appropriate partitioning (or none, for small dimensions).

**Concepts.**

- `multiLine=True` for JSON arrays. The two `.json.gz` files are JSON
  *arrays* (not JSON Lines), so `spark.read.json(...)` defaults to
  one-record-per-line and reads them as one big null row. Adding
  `.option("multiLine", "true")` is the fix.
- `explode()` and array flattening — `customer-profiles.typical_categories`
  is a 2–5 element array. Whether to flatten now or later is a design
  call (recommendation: keep it as an array column in `/curated`,
  flatten in `/analytics` if needed).
- Dimension tables (`merchant-directory`, ~10K rows) don't need
  partitioning — they fit comfortably in one Parquet file and are
  always broadcast-joined.

**What to build.** Four small jobs (or one driver with four functions):

| Source | Output path | Partition by | Notes |
|---|---|---|---|
| `device-fingerprints.csv.gz` | `/curated/device-fingerprints/` | `device_type` | mobile / desktop / tablet |
| `customer-profiles.json.gz` | `/curated/customer-profiles/` | none (small) | use `multiLine=true`, keep `typical_categories` as array |
| `merchant-directory.csv.gz` | `/curated/merchant-directory/` | none | 10K rows = single file |
| `fraud-reports.json.gz` | `/curated/fraud-reports/` | `fraud_type` | use `multiLine=true` |

Files to add: `jobs/curate_devices.py`, `jobs/curate_customers.py`,
`jobs/curate_merchants.py`, `jobs/curate_fraud_reports.py`.

**Verify.**

```sh
for ds in transactions device-fingerprints customer-profiles merchant-directory fraud-reports; do
  echo "=== $ds ==="
  docker compose exec namenode hdfs dfs -du -h /curated/$ds
done
# Expect: customer-profiles ≈ 5–10 MB, merchant-directory < 1 MB, etc.
# Compression ratio vs landing should be ~3–5×.
```

---

## Step 4 — Build the master enriched fact in `/analytics`

**Goal.** Join transactions with all four other datasets into one wide
table that downstream feature engineering and detection can read.

**Concepts.**

- **Broadcast join** — when one side is small (< ~10–100 MB) Spark can
  ship it to every executor and skip the shuffle. `customer-profiles`
  (~10 MB Parquet) and `merchant-directory` (~1 MB) are perfect
  candidates. Use `broadcast(df)` from `pyspark.sql.functions`.
- **LEFT vs INNER join.** Device fingerprints and fraud reports are
  *sparse* — not every txn has them. Use LEFT join with the txn fact
  on the left so we don't drop rows.
- **Label leakage.** `fraud-reports.timestamp` happens *after* the txn.
  That's fine for the batch label, but you must NEVER use this column
  as a model feature for streaming predictions. The label column is
  `confirmed_fraud = (resolution == 'confirmed_fraud')`.

**What to build.**

- `jobs/build_enriched_fact.py`:
  - Read all five `/curated/*` Parquet datasets.
  - Join: `transactions` left-join `devices` on `txn_id`,
    left-join `fraud_reports` on `txn_id`,
    join `broadcast(customers)` on `card_id`,
    join `broadcast(merchants)` on `merchant_id`.
  - Add label column `confirmed_fraud`.
  - Write `/analytics/transactions_enriched/` partitioned by `dt`.

**Verify.**

```sh
# fraud rate across the whole dataset should be ~1.5–2.5%
docker compose exec spark-master /opt/spark/bin/spark-submit ... \
  jobs/check_fraud_rate.py
# Should print:  fraud_rate ≈ 0.018  (≈ 18,000 confirmed fraud rows)
```

---

## Step 5 — Customer behavioral baselines (feature store)

**Goal.** Compute one row per `card_id` with statistics describing that
customer's *normal* behavior. These are the features we'll deviate
from to detect fraud.

**Concepts.**

- **Aggregations as features.** Mean, stddev, percentiles of historical
  amounts. Set/array of countries and merchant categories the customer
  has historically used.
- **`approx_count_distinct` and `percentile_approx`** — exact distinct
  counts on 1M rows are expensive; approximate variants are usually
  ~1% off and 10× faster.
- **Why a separate feature store.** The streaming job in Step 8 will
  broadcast-load this small Parquet to score every incoming txn. The
  goal is: cheap to read, fast to join, no PII beyond what scoring
  needs.

**Features to compute per `card_id` (suggested set, not exhaustive — the
rubric rewards creativity here):**

- `avg_amount`, `stddev_amount`, `p95_amount`, `p99_amount`
- `txn_count`, `unique_merchant_count`, `unique_country_count`
- `seen_countries` (array of distinct countries used)
- `seen_categories` (array of distinct merchant categories)
- `home_country` (from customer-profiles directly)
- `avg_monthly_spend` (from customer-profiles directly)
- `pct_intl`, `pct_online` (channel/intl ratios)

**What to build.**

- `jobs/build_customer_features.py`:
  - Read `/analytics/transactions_enriched/`.
  - Group by `card_id` with aggregations above.
  - Join in static fields from `/curated/customer-profiles/`.
  - Write `/analytics/customer_features/` (no partitioning, ~100K rows).

**Verify.**

```sh
# Spot-check: amount distribution should look reasonable
docker compose exec spark-master spark-submit ... -e \
  "spark.read.parquet('/analytics/customer_features/').describe('avg_amount','txn_count').show()"
# avg_amount should be in the $50–$500 range; txn_count median ≈ 10
```

---

## Step 6 — Offline detection: rules + a simple ML baseline

**Goal.** Apply the explicit rules from the brief and (optionally) train
a small classifier, then evaluate against the noisy `confirmed_fraud`
label using *imbalance-aware* metrics.

**Concepts.**

- **Rule-based vs ML.** Rules are interpretable and ship instantly;
  ML adds lift but needs explanation. Real fraud teams ship rules
  first, then layer ML.
- **Class imbalance.** ~2% positive class means accuracy is meaningless
  ("predict not-fraud always" → 98% accuracy). Use precision, recall,
  F1, and PR-AUC. Confusion matrix + cost-weighted metric (false
  negatives are expensive).
- **Label noise.** Even `confirmed_fraud` rows include ~15% mislabels
  (the generator plants false alarms). A perfect classifier can't get
  100% recall.

**What to build.**

- `jobs/score_offline.py`:
  - Read enriched fact + customer features, join.
  - Compute rule columns (per the brief):
    - `rule_high_amount` = amount > 3 × `avg_monthly_spend` / 30
    - `rule_velocity` = (5+ txns from this card within any 10-min window
      that day) — use `Window.partitionBy("card_id").orderBy("ts")
      .rangeBetween(-600, 0)` and `count`
    - `rule_intl_mismatch` = is_international AND home_country == only
      country ever seen
    - `rule_unknown_device_vpn` = NOT is_known_device AND is_vpn
    - `rule_high_risk_merchant` = merchant.risk_score >= 8
  - Compute `risk_score` = sum of triggered rules (0–5).
  - Compute `predicted_fraud` = risk_score >= 2 (start point; tune).
  - Compute `precision`, `recall`, `f1`, `pr_auc` against
    `confirmed_fraud`.
- (Optional, later) `jobs/train_simple_model.py`: same features →
  Spark MLlib LogisticRegression or RandomForest → compare metrics.

**Verify.**

- Print confusion matrix and PR-curve. Reasonable expectation: 2–3
  rules together get you ~70% recall at ~30% precision before tuning.
- Translate to dollars: assume avg $200 per fraud txn caught ⇒
  `recall × ~18000 confirmed × $200 ≈ $X` prevented.

---

## Step 7 — Kafka producer: replay transactions as a stream

**Goal.** A Python script that reads `transactions.csv.gz` and pushes
each row to Kafka topic `transactions`, keyed by `card_id`.

**Concepts.**

- **Why key by `card_id`.** Same card → same Kafka partition →
  preserved ordering. Velocity windows in Step 8 need this guarantee.
- **Speed-vs-realism trade-off.** The 1M txns span 6 months of wall
  time. Three replay modes:
  1. **Throttled flat rate** (e.g. 200 txn/sec) — finishes in ~80
     minutes, simple to reason about.
  2. **Real-time replay** — emit at the timestamp's wall-clock offset
     (×N speedup factor). Closer to production; harder to debug.
  3. **Burst mode** — pump as fast as possible. Stress-tests the
     downstream but doesn't exercise windowing.
  Recommendation: ship mode 1 first, mode 2 as a `--speedup 60` flag.
- **Idempotence and at-least-once.** `enable_idempotence=True` on the
  producer prevents duplicates within a session; the consumer side
  has to dedupe across producer restarts (we won't bother for the
  class project).

**What to build.**

- `producers/replay_transactions.py`:
  - CLI args: `--rate <txn/sec>`, `--limit <max>` (for testing).
  - Read csv.gz with stdlib `csv` + `gzip`, no pandas needed.
  - Use `confluent-kafka` or `kafka-python` (whichever you prefer; both
    work). Bootstrap servers: `localhost:9092` from the host.
  - Send JSON value, key = `card_id` bytes.

**Verify.**

```sh
# In one terminal, watch the topic in kafdrop:
open http://localhost:9001

# In another, run:
python producers/replay_transactions.py --rate 100 --limit 1000

# Confirm in kafdrop: 'transactions' topic has 1000 messages,
# distributed across partitions, keyed by card_id.
```

---

## Step 8 — Spark Structured Streaming: real-time scoring

**Goal.** A long-lived Spark job that reads `transactions`, looks up the
customer-features broadcast, applies the rules from Step 6 *plus* a
true real-time velocity check, and emits to two output topics:
`fraud-scores` (every txn) and `fraud-alerts` (only when score ≥ 2).

**Concepts.**

- **Structured Streaming model** — micro-batch under the hood (default
  trigger ~250ms), but the API is the same DataFrame as batch. The
  same `groupBy(window(...)).count()` works on streams.
- **Watermark.** Tells Spark how late events can arrive before being
  dropped. For our generator, set `withWatermark("event_time", "2 minutes")`.
- **Stateful aggregation.** Per-card velocity over a 10-min sliding
  window is genuine streaming state — Spark holds it in the state
  store between micro-batches. This is the heaviest concept in the
  project; expect to debug.
- **Why broadcast the customer features.** Streaming can't shuffle-join
  to a 100K-row Parquet on every micro-batch. Read once, broadcast,
  reuse forever. (For a class project — production would use a
  proper feature store.)
- **Two outputs from one query.** Use `foreachBatch` and write to two
  topics in the same batch, OR run two queries. `foreachBatch` is
  simpler.

**What to build.**

- `jobs/stream_score.py`:
  - Source: `kafka.bootstrap.servers=kafka:9094`, topic `transactions`.
  - Parse JSON value into typed DataFrame.
  - Broadcast-load `/analytics/customer_features/`.
  - Compute rule columns (same as Step 6 for the static rules);
    add the streaming-only velocity rule via window aggregation.
  - `risk_score` and `predicted_fraud` as before.
  - `foreachBatch`: write all rows to `fraud-scores`; filter and
    write to `fraud-alerts`. Alert payload:

    ```json
    {"txn_id": "...", "card_id": "...", "risk_score": 3,
     "triggered_rules": ["high_amount", "velocity", "vpn_unknown"],
     "recommended_action": "block"}
    ```

  - `recommended_action`: score 2 → "review", 3+ → "block".
- Submit with the Kafka connector package:

  ```sh
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1
  ```

  (See the existing note in `CLAUDE.md` — connector is not pre-baked.)

**Verify.**

```sh
# Terminal 1: start the streaming job (it will run forever).
docker compose exec spark-master /opt/spark/bin/spark-submit \
  --master spark://spark-master:7077 \
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
  /opt/jobs/stream_score.py

# Terminal 2: replay a small batch.
python producers/replay_transactions.py --rate 50 --limit 5000

# Terminal 3: tail the alert topic.
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9094 --topic fraud-alerts --from-beginning

# Expect ~50–100 alerts, each with txn_id + triggered rules.
```

---

## Step 9 — Airflow orchestration

**Goal.** Two DAGs:

- `daily_batch` — chains Steps 1 → 5 with quality gates.
- `streaming_monitor` (every 15 min) — checks Kafka consumer lag
  and that `fraud-alerts` is producing within expected bounds.

**Concepts.**

- `SparkSubmitOperator` vs `BashOperator`. The Spark submit operator
  needs Spark on PATH inside the Airflow container, which we don't
  have. **For this project, use `BashOperator` + `docker compose exec
  spark-master spark-submit ...`** — simpler, no extra deps. (You
  can revisit later if you set up a SparkConnect host.)
- **Task dependencies and `XCom`** — pass small values (row counts,
  timestamps) between tasks via `xcom_push` / `xcom_pull`.
- **Quality gates as `ShortCircuitOperator` or `BranchPythonOperator`.**
  If fraud rate this batch is outside [0.5%, 5%], short-circuit
  the downstream "publish" task and alert.
- **Sensors.** `ExternalTaskSensor` if you want monitor → batch
  coordination; not strictly needed for class scope.

**What to build.**

- `airflow/dags/daily_batch.py`:

  ```text
  land_data → curate_transactions → curate_others (parallel)
            → build_enriched_fact → build_customer_features
            → run_offline_scoring → quality_gates → publish_metrics
  ```

- `airflow/dags/streaming_monitor.py`: reads Kafka consumer-group
  offsets and topic high-watermarks via the Kafka admin API; alerts
  if lag > N for X minutes, or alert rate is 0 for X minutes.
- Schedule: `daily_batch` at `0 2 * * *`; monitor at `*/15 * * * *`.

**Verify.**

```sh
# Trigger a manual run from the UI at http://localhost:8081
# Then on the CLI:
docker compose exec airflow-scheduler airflow dags state daily_batch <date>
# Expect: success
```

---

## Step 10 — Analysis notebook + business-impact report

**Goal.** Turn the technical pipeline output into the seven business
answers the brief asks for. This is what the rubric calls out as
"business impact quantification".

**Concepts.**

- The brief explicitly weighs **"connecting findings to dollars"** —
  e.g., "velocity detection catches X cases/month worth $Y in
  prevented losses, with Z% false positive rate." Every chart should
  end in a dollar number or a percentage tied to action.
- Class-imbalance-aware visuals: PR curves > ROC curves; cost-tuned
  threshold; per-segment breakdown (channel, country, merchant cat).

**What to build.**

- `notebooks/analysis.ipynb` (or `notebooks/analysis.py` + Markdown
  rendering — your choice). One section per business question:
  1. Top fraud features (feature importance from Step 6 model).
  2. Merchant risk score update (compare brief's "stale 2-year-old"
     scores to actual fraud rates from `/analytics/transactions_enriched`).
  3. Velocity attack: distribution of attack sizes, $ exposure.
  4. Device + channel risk: VPN correlation, OS breakdown.
  5. Geographic risk: home-country × txn-country fraud heatmap.
  6. False-positive analysis: which customer segments flag falsely.
  7. Customer behavior anomalies: feature deltas for confirmed-fraud
     rows vs population.
- Read directly from `/analytics/*` via PySpark in the notebook
  (the `pyspark` package is in the Spark image; mount the repo into
  a Jupyter container, or run Spark locally with same `HADOOP_CONF_DIR`).

**Verify.**

- Notebook runs end-to-end with no manual intervention.
- Each section ends with a quantified statement: "$X prevented",
  "Y% false positive lift", "Z merchants flagged for review".

---

## What "done" looks like

Mapping back to the rubric in `docs/scenario.md`:

| Rubric criterion | Where it lands |
|---|---|
| Stage 1 — Data Lake | Steps 1, 2, 3 |
| Stage 2 — Spark Batch | Steps 4, 5, 6 |
| Stage 3 — Streaming | Steps 7, 8 |
| Stage 4 — Orchestration | Step 9 |
| Feature engineering depth | Step 5 (creativity), Step 6 (rules), Step 10 (analysis) |
| Class-imbalance awareness | Step 6 (PR-AUC, threshold tuning), Step 10 (visuals) |
| Real-time architecture | Step 8 (watermark + state + two outputs) |
| Business-impact framing | Step 10 (dollars per question) |

**Rough effort estimate (assuming the project is the user's full focus):**

- Step 1–3: ~3–4 hours (mostly mechanical once Step 2's pattern is set).
- Step 4–5: ~3 hours (joins + windowed aggs).
- Step 6: ~2–4 hours (rules quick, ML optional).
- Step 7: ~2 hours.
- Step 8: ~4–6 hours (this is where the most learning happens).
- Step 9: ~2–3 hours.
- Step 10: ~3–6 hours depending on polish.

**Total: ~25–35 hours of focused work.** Spread over 2–3 weeks of
evenings is comfortable; over a single weekend is tight.

---

## How to use this plan

- One step per session — no skipping ahead. Each builds on the
  previous step's outputs at known HDFS / topic paths.
- At the end of each step, run **exactly** the command in its
  **Verify** block before moving on. If verification fails, debug
  out loud rather than working around the symptom.
- When introducing a concept marked in the **Concepts** list, take
  a moment to read about it (Spark docs / the `docs/services.md`
  file) before writing the code — the goal of this project is
  learning, not throughput.
