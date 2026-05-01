# FinPulse-Fraud — End-to-End Implementation Plan

## Context

The infrastructure is up and `make smoke && make smoke-airflow &&
make smoke-pinot && make smoke-flink` all pass. The full local stack is:

- **HDFS** (1 NameNode + 2 DataNodes) — dim landing + Spark analytics outputs.
- **Spark** (1 master + 2 workers) — batch consumer of Kafka `transactions`
  - HDFS dim joins + Pinot offline-segment generation.
- **Kafka** (single broker, KRaft) — source of truth for the transaction
  fact stream. Three topics: `transactions`, `transactions-scored`,
  `fraud-alerts`.
- **Flink** (1 jobmanager + 1 taskmanager, 4 slots) — streaming consumer
  of Kafka `transactions`, writes scored events back to Kafka.
- **Pinot** (zookeeper + controller + broker + server) — OLAP serving
  layer; will host the `transactions_scored` hybrid table (real-time
  from Kafka + offline from HDFS).
- **Superset** — BI front-end on Pinot via the `pinotdb` SQLAlchemy driver.
- **Airflow** (LocalExecutor) — orchestrates the daily Spark batch DAG and
  monitors the long-running Flink job.

The 5 source datasets (1M transactions, 100K customers, 600K device
sessions, 15K fraud reports, 10K merchants) are sitting in `data/` as
gzipped CSV/JSON.

The class brief in [`docs/scenario.md`](../scenario.md) defines four
stages (HDFS Lake → Spark Batch → Kafka Streaming → Airflow Orchestration)
and seven business questions to answer. The rubric weights **feature
engineering depth, class-imbalance awareness, real-time architecture
quality, and dollar-impact framing** — not pure accuracy.

This plan turns that brief into 12 small, observable steps so each new
concept lands one at a time. Steps 1–8 + 11–12 implement the four
stages; Steps 9–10 add a Pinot + Superset analytics-serving layer
(per the Robinhood pattern in
[`docs/odsc/robinhood_infrastructure.md`](../odsc/robinhood_infrastructure.md))
that the brief doesn't strictly require but makes the real-time
architecture credit easier to demonstrate. Every step ends with
something runnable and a single command to verify it. The end-to-end
shape lives in [`docs/plans/dataflow.md`](dataflow.md) — read that
diagram before starting any step.

**Conventions used throughout the steps:**

- All HDFS paths use the cluster-internal scheme `hdfs://namenode:9000/...`.
  Three top-level zones: `/landing` (raw, immutable), `/curated` (Parquet,
  cleaned), `/analytics` (joined / feature-engineered, query-optimised).
  **Transactions are not in any of these zones** — they live in Kafka
  topic `transactions` only.
- Spark batch jobs run as `spark-submit --master spark://spark-master:7077`
  via `docker compose exec spark-master ...`. The Kafka source connector
  is not pre-baked, so any job that reads Kafka needs
  `--packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1`. New
  jobs land in `jobs/` next to the existing `smoke_spark.py`.
- Flink streaming jobs run via `docker compose exec flink-jobmanager
  flink run -d /opt/flink/usrlib/<artifact>` with `./src/consumer/`
  bind-mounted at `/opt/flink/usrlib`. The Kafka connector ships with
  the Flink image — no `--packages` needed.
- Pinot tables are registered via `POST /schemas` and `POST /tables`
  against `http://pinot-controller:9000` (or host port 9100). Schema +
  tableconfig JSONs land under `utils/pinot/`.
- Superset reads Pinot via SQLAlchemy URL
  `pinot://pinot-broker:8099/query/sql?controller=http://pinot-controller:9000`.
- Each step writes its outputs at known paths so the next step can read
  them; if you re-run the whole pipeline from scratch,
  `make nuke && make up` wipes all named volumes (HDFS / Kafka / Pinot
  segments / Superset metadata / Flink checkpoints / Postgres).

---

## At a glance

| Step | Output                                                                          | New concept                                |
| ---- | ------------------------------------------------------------------------------- | ------------------------------------------ |
| 0    | infra healthy                                                                   | (done)                                     |
| 1    | `/landing/{customers,merchants,devices,fraud-reports}/` raw `.gz`               | zones, replication factors (txns NOT here) |
| 2    | *(none — txns flow only through Kafka, no `/curated/transactions/`)*            | Kafka as source-of-truth for facts         |
| 3    | `/curated/{devices,customers,merchants,fraud-reports}/`                         | JSON `multiLine`, dim vs fact              |
| 4    | `/analytics/transactions_enriched/` (Spark batch-reads Kafka + HDFS dims)       | Spark Kafka batch source, broadcast joins  |
| 5    | `/analytics/customer_features/`                                                 | aggregations as features, approx aggs      |
| 6    | rule-flagged + (optional) ML scored                                             | class imbalance, PR-AUC                    |
| 7    | Kafka producer replaying CSV → `transactions`                                   | partition keys, replay modes               |
| 8    | **Flink** `stream_score` → Kafka `transactions-scored` / `fraud-alerts`         | event-time, watermarks, broadcast state, exactly-once |
| 9    | Pinot **hybrid table** (`transactions_scored`): real-time from Kafka + offline from HDFS | OLAP segments, hybrid tables, deep store   |
| 10   | Superset dashboards on the Pinot hybrid table                                   | BI on OLAP, real-time charts               |
| 11   | `daily_batch` + `streaming_monitor` DAGs                                        | quality gates, BashOperator pattern        |
| 12   | `notebooks/analysis.ipynb` answering the 7 business questions                   | $-impact framing                           |

**Architectural note.** `transactions` is a Kafka-only stream. It is
never landed in `/landing/transactions/` or curated to
`/curated/transactions/`. Both Spark (batch) and Flink (streaming)
subscribe to the same `transactions` topic. The Kafka topic is the
single source of truth; HDFS holds dimensions + Spark-derived
analytics outputs.

**Suggested execution order.** Steps don't strictly need to follow
the numbered order, but a dependency-respecting sequence is:
`1 → 3 → 7 → 4 → 5 → 6 → 8 → 9 → 10 → 11 → 12`. Step 7 (producer)
must run before Step 4 (Spark batch read of Kafka) so the topic has
data to consume.

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

## Step 1 — Land the dimension datasets into HDFS

**Goal.** Get the four dimension / fact-secondary `.gz` files from
`data/` into `/landing/<dataset>/` on HDFS, **untouched**. The landing
zone is the immutable record of what the upstream provider gave us —
no transforms, no schema changes.

**`transactions.csv.gz` is deliberately NOT in this step.**
Transactions are a Kafka-only stream (Step 7). The single source of
truth for the transaction fact is the Kafka topic `transactions`, not
HDFS. There will be no `/landing/transactions/` and no
`/curated/transactions/`. Both batch (Spark, Step 4) and streaming
(Flink, Step 8) consumers read from Kafka.

**Concepts you'll meet for the first time.**

- HDFS zone pattern (landing / curated / analytics) and why
  immutability matters for audit + reproducibility — for *dimensions*.
  Facts get the same property from Kafka's append-only log.
- Per-directory replication factors. The brief asks for higher
  replication on `fraud-reports` (audit) and `customer-profiles`
  (regulatory).
- The `hdfs dfs -put` and `hdfs dfs -setrep` commands.

**What to build.**

- `jobs/land_data.py` — a small PySpark *or* plain Python+`hdfs` CLI
  driver that, idempotently, creates the zone dirs and uploads the
  four dimension files. Plain `bash` via the namenode container is
  also fine for this step — keep it minimal.
- Suggested layout:

  ```text
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
# /landing/transactions/ should NOT exist.
```

---

## Step 2 — *removed*

Originally "Curate transactions to partitioned Parquet". Dropped
because transactions never land in HDFS — they live only in Kafka,
and Spark's batch read in Step 4 consumes them directly via the Kafka
batch source (`spark.read.format("kafka").option("startingOffsets",
"earliest").option("endingOffsets","latest")`).

Step numbering is preserved so cross-references in older docs and
commits still resolve.

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
  - **Read transactions from Kafka in batch mode.** Spark's Kafka
    source supports a batch read by passing a starting + ending
    offset range — Spark consumes the entire topic, parses the JSON
    value column into a typed DataFrame, and you treat it like any
    other DataFrame.

    ```python
    txns_raw = (spark.read.format("kafka")
                .option("kafka.bootstrap.servers", "kafka:9094")
                .option("subscribe", "transactions")
                .option("startingOffsets", "earliest")
                .option("endingOffsets", "latest")
                .load())
    txns = (txns_raw
        .selectExpr("CAST(value AS STRING) AS json")
        .select(from_json("json", txn_schema).alias("t")).select("t.*"))
    ```

  - **Read the four dimensions from `/curated/*` Parquet** as before.
  - Join: `txns` left-join `devices` on `txn_id`,
    left-join `fraud_reports` on `txn_id`,
    join `broadcast(customers)` on `card_id`,
    join `broadcast(merchants)` on `merchant_id`.
  - Add label column `confirmed_fraud`.
  - Write `/analytics/transactions_enriched/` partitioned by `dt`.

**Prerequisite.** The `transactions` topic must already have data
in it — run Step 7 (the producer) at least once before this. The
recommended order is `1 → 3 → 7 → 4 → ...`.

**Topic retention.** Make sure `transactions` is configured with
long retention (e.g. `--config retention.ms=-1` to disable
expiration, or 30+ days) so a re-run of Step 4 weeks later still
sees every event. Default 7-day retention will silently truncate
the historical record otherwise.

**Why batch-read Kafka instead of file-read the gz.** Kafka is the
single source of truth for transactions; if Step 4 read the `.gz`
directly, batch and streaming consumers would diverge whenever the
producer was replayed at a different rate, with late events, or
after a fix. Batch-reading Kafka guarantees the same timeline both
consumers saw.

**Verify.**

```sh
# fraud rate across the whole dataset should be ~1.5–2.5%
docker compose exec spark-master /opt/spark/bin/spark-submit \
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
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

- `src/producer/replay_transactions.py`:
  - CLI args: `--rate <txn/sec>`, `--limit <max>` (for testing).
  - Read csv.gz with stdlib `csv` + `gzip`, no pandas needed.
  - Use `confluent-kafka` or `kafka-python` (whichever you prefer; both
    work). Bootstrap servers: `localhost:9092` from the host.
  - Send JSON value, key = `card_id` bytes.
- One-time topic config — long retention so Step 4 can batch-read
  the full topic weeks later. Default Kafka retention is 7 days,
  which silently truncates the historical record. Run once before
  the first producer invocation:

  ```sh
  docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 \
    --create --if-not-exists --topic transactions \
    --partitions 6 --replication-factor 1 \
    --config retention.ms=-1 \
    --config segment.bytes=104857600
  ```

  `retention.ms=-1` disables time-based deletion; the topic acts as
  the long-term system of record for transactions, just like the
  Robinhood pattern (their `transactions` Kafka topic feeds both
  Pinot real-time ingest and the nightly Spark reconciliation).

**Verify.**

```sh
# In one terminal, watch the topic in kafdrop:
open http://localhost:9001

# In another, run:
python src/producer/replay_transactions.py --rate 100 --limit 1000

# Confirm in kafdrop: 'transactions' topic has 1000 messages,
# distributed across partitions, keyed by card_id.
```

---

## Step 8 — Flink: real-time scoring (Kafka → Flink → Kafka)

**Goal.** A long-lived **Flink** application that reads `transactions`,
looks up the customer-features broadcast state, applies the rules
from Step 6 *plus* a true real-time velocity check, and emits to two
output topics:

- `transactions-scored` — every txn with its rule columns and risk
  score (this is the audit-grade source-of-truth stream and the
  topic Pinot's real-time table will consume in Step 9).
- `fraud-alerts` — only `risk_score >= 2`, payload tuned for live
  ops dashboards.

**Why Flink instead of Spark Structured Streaming.** Robinhood's talk
([`docs/odsc/robinhood_infrastructure.md`](../odsc/robinhood_infrastructure.md))
makes the case directly: only Flink is event-time native, true-streaming
(not micro-batch), stateful at scale, exactly-once with two-phase
commit to Kafka, **and** millisecond latency — simultaneously.
Compliance-grade financial analytics (which fraud is) needs event-time
bucketing or you misattribute trades to the wrong window.

**Prerequisite (one-time).** Add Flink to the docker-compose stack:
`flink-jobmanager` + `flink-taskmanager` (image `flink:1.19-scala_2.12`),
joined to the same network as Kafka, HDFS, and Pinot. Mount
`./src/consumer/stream_score:/opt/flink/usrlib` for job submission.

**Concepts.**

- **Event time vs processing time.** Flink windows on the txn's own
  `timestamp`, not on arrival time. Set the watermark generator to
  `forBoundedOutOfOrderness(Duration.ofMinutes(2))` — this matches
  Robinhood's `T − 5min` tolerance, scaled down for our toy stream.
- **Side outputs for late data.** Events past the watermark do not
  go on `transactions-scored`; they go to a side output stream that
  Spark folds into the offline Pinot segments at night. This is the
  "very late data → Spark corrects offline" pattern from the talk.
- **Broadcast state.** Customer features are a slowly-changing
  *control stream*, not a fact stream — they should be broadcast to
  every parallel task and refreshed periodically, not stream-stream
  joined. Read `/analytics/customer_features/` and
  `/curated/merchant-directory/` once on startup; rebroadcast on a
  scheduled timer (e.g. once per hour) to pick up nightly batch
  updates.
- **Keyed sliding-window velocity.**
  `keyBy(card_id).window(SlidingEventTimeWindows.of(Time.minutes(10),
  Time.minutes(1)))` — same key (`card_id`) all the way from Kafka
  through Flink to Pinot, so a card's window state, scored events,
  and Pinot segment all live on the same partition.
- **Exactly-once with Kafka.** Configure the Kafka sink with
  `DeliveryGuarantee.EXACTLY_ONCE` and a transactional ID prefix,
  and align `execution.checkpointing.interval` to `30s`. Both
  output topics participate in the same Flink checkpoint, so they
  never disagree about whether a txn was processed.
- **Watch the "where joins really belong" rule.** No stream-stream
  joins. Velocity is keyed (one stream). Customer/merchant lookups
  are broadcast state (control stream). Fact ↔ fact joins, if any
  are ever needed, will go in Pinot's multi-stage engine at query
  time, not in Flink.

**What to build.**

- `src/consumer/stream_score/` — a small Flink Java/Scala project
  (or Python via PyFlink if you want to stay in Python; Java is
  closer to how Robinhood and most production shops run it):
  - Kafka source on `transactions` (bootstrap `kafka:9094`),
    `card_id` as the deserialized key, JSON value schema.
  - Two HDFS-backed broadcast streams (customer features, merchant
    directory), refreshed via processing-time timers.
  - `KeyedBroadcastProcessFunction` that emits one
    `transactions-scored` record per input event, plus an alert via
    side output if `risk_score >= 2`.
  - Two Kafka sinks (`transactions-scored`, `fraud-alerts`),
    exactly-once, transactional.
  - Side output for events past the watermark, written to
    `hdfs://namenode:9000/analytics/late_events/` for the next
    Spark batch run to pick up.
- Alert payload (unchanged from before):

  ```json
  {"txn_id": "...", "card_id": "...", "event_time": "...",
   "risk_score": 3,
   "triggered_rules": ["high_amount", "velocity", "vpn_unknown"],
   "recommended_action": "block"}
  ```

  `recommended_action`: score 2 → `"review"`, 3+ → `"block"`.

**Verify.**

```sh
# Terminal 1: submit the Flink job (runs until cancelled).
docker compose exec flink-jobmanager flink run \
  -d /opt/flink/usrlib/stream_score.jar

# Terminal 2: replay a small batch.
python src/producer/replay_transactions.py --rate 50 --limit 5000

# Terminal 3: confirm both output topics fill up.
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9094 --topic transactions-scored \
  --from-beginning --max-messages 5
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9094 --topic fraud-alerts \
  --from-beginning --max-messages 5

# Terminal 4: trigger a savepoint + restart to confirm exactly-once.
docker compose exec flink-jobmanager flink stop --savepoint /tmp/sp <jobid>
docker compose exec flink-jobmanager flink run -s /tmp/sp ...
# Re-tail transactions-scored — there should be NO duplicate txn_ids.
```

Expected on a 5K replay: ~50–100 alerts on `fraud-alerts`,
~5,000 records on `transactions-scored`, zero duplicate `txn_id`s
across a savepoint-resume.

---

## Step 9 — Pinot hybrid table: real-time from Kafka + offline from HDFS

**Goal.** Stand up one logical Pinot table `transactions_scored`
backed by two physical tables — a real-time table fed by Kafka topic
`transactions-scored`, and an offline table built nightly by Spark
from `/analytics/transactions_enriched/`. The Pinot broker unions
them at query time; queries get sub-second latency on fresh data and
audit-grade correctness on historical data.

This is the Robinhood **hybrid table** pattern from slide #13 of the
ODSC talk, applied at our toy scale.

**Concepts.**

- **Pinot segments** are the storage atom — immutable, columnar,
  ~100 MB–1 GB each. Real-time path: Pinot's server tails Kafka and
  builds an in-memory consuming segment, then seals it to disk on
  a size/time threshold. Offline path: Spark builds segments
  directly from Parquet and registers them with the controller.
- **Schema vs table-config.** Pinot needs two JSONs registered with
  the controller: a **schema** (column names + types + the
  designated time column) and a **table-config** per physical table
  (real-time vs offline). The hybrid table is just two table-configs
  sharing one schema and one logical name.
- **Time column + cutover.** The time column on the schema is the
  txn `event_time`. The broker uses the offline-table's max time as
  the **cutover boundary**: queries for `event_time < cutover` are
  served from offline, `>= cutover` from real-time. This is the
  diagram on slide #13.
- **Indexing strategy.** Pinot's per-column, per-segment indexes
  let you mix and match — a sorted index on `dt`, an inverted index
  on `merchant_category`, a range index on `amount`, a star-tree
  for predictable p99 on the most-asked aggregate (probably
  `count(*) GROUP BY merchant_category, dt`). Define them in the
  table-config; rebuild segments to apply.
- **Deep store.** For the class project, the local-disk deep store
  on the existing `pinot-controller-data` named volume is fine.
  Production would point `pinot.controller.storage.factory.class`
  at HDFS so any Pinot server can rehydrate any segment.

**What to build.**

- `utils/pinot/transactions_scored.schema.json` — Pinot schema
  (columns, types, time field, primary key = `txn_id` for upsert
  support).
- `utils/pinot/transactions_scored.realtime.tableconfig.json` —
  real-time table consuming `kafka:9094` topic
  `transactions-scored`, partition by `card_id`, replication 1.
- `utils/pinot/transactions_scored.offline.tableconfig.json` —
  offline table, segments under `/var/pinot/controller/data/`,
  pulled in via the Spark batch job below.
- `utils/pinot/load_tables.sh` — registers the schema and both
  table-configs against the controller (`POST /schemas`,
  `POST /tables`).
- `jobs/build_pinot_offline_segments.py` — nightly Spark job that
  reads `/analytics/transactions_enriched/` plus the late-event side
  output from Step 8, generates Pinot segment files via the Pinot
  Spark plugin (or the controller's `/segments` upload endpoint),
  and uploads them to the offline table.
- Wire `build_pinot_offline_segments.py` into the Airflow daily DAG
  in Step 11 as the last task after `run_offline_scoring`.

**Verify.**

```sh
# 1. Tables registered + healthy:
curl -fsS http://localhost:9100/tables | jq '.tables'
# expect: ["transactions_scored_REALTIME", "transactions_scored_OFFLINE"]

# 2. Real-time path catching new events from Kafka:
python src/producer/replay_transactions.py --rate 100 --limit 1000
sleep 10
curl -fsS -X POST http://localhost:8099/query/sql \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT count(*) FROM transactions_scored WHERE event_time > ago(\"PT1M\")"}'
# expect a non-zero count within ~5s of producing.

# 3. Offline path catching reconciled segments:
docker compose exec spark-master /opt/spark/bin/spark-submit \
  --master spark://spark-master:7077 \
  /opt/jobs/build_pinot_offline_segments.py --date 2025-03-14
curl -fsS -X POST http://localhost:8099/query/sql \
  -d '{"sql":"SELECT count(*) FROM transactions_scored WHERE dt = '\''2025-03-14'\''"}'
# expect: count matches the row count for that day in HDFS.

# 4. Hybrid cutover working (the same query returns both halves):
curl -fsS -X POST http://localhost:8099/query/sql \
  -d '{"sql":"SELECT dt, count(*) FROM transactions_scored GROUP BY dt ORDER BY dt"}'
# expect: contiguous days, no gap at the offline/realtime cutover.
```

---

## Step 10 — Superset dashboards on Pinot

**Goal.** Bring Superset up, wire it to the Pinot hybrid table from
Step 9, and build three dashboards that each exercise a different
property of the architecture.

### 10a — Spin up Superset and confirm it's healthy

Superset itself comes up as part of `make up` (containers
`superset-init` runs once → `superset` long-lived gunicorn). On a
*fresh* volume the first start takes longer because:

- The `superset-init` script (`docker/superset/superset-init.sh`)
  runs `pip install -r /app/docker/requirements-local.txt` (installs
  `pinotdb`), then `superset db upgrade`, `superset fab create-admin`,
  `superset init`. ~30–60 s.
- The `superset` container's bootstrap re-runs the same `pip install`
  on every start (site-packages live in the image fs, not a named
  volume), then `exec`s `run-server.sh` (gunicorn). Adds ~15–20 s
  per restart. Plan: `start_period: 90s` on the healthcheck.

```sh
# Bring it up (idempotent — no-op if already healthy):
make up                                # full stack
# or, if you only want Pinot + Superset for BI work:
make up-bi                             # Pinot quartet + Superset only

# Wait for healthy (about 60–90s on first run):
docker compose ps superset             # STATUS should say "(healthy)"
docker compose logs --tail=50 superset-init  # confirm "[superset-init] done"
curl -fsS http://localhost:8088/health # → "OK"
```

If `superset-init` exited non-zero, the long-running `superset`
container will not start (compose `service_completed_successfully`
gate). Common causes: `pinotdb` install failure (network), corrupt
SQLite metadata DB on the named volume (`make nuke` to wipe).

### 10b — Wire Superset to the Pinot hybrid table

Once Superset is healthy:

1. Open <http://localhost:8088>. Login `admin` / `admin` (set in
   `superset_config.py` via the `ADMIN_*` env vars on `superset-init`).
2. **Settings → Database Connections → + Database**.
3. Pick **Other** in the dropdown. SQLAlchemy URI:

   ```text
   pinot://pinot-broker:8099/query/sql?controller=http://pinot-controller:9000
   ```

   Display name: `Pinot — finpulse`. Click **Test Connection** — must
   say "Connection looks good!" before saving. (If it fails, the
   `pinotdb` driver is most likely missing — check
   `docker compose logs superset | grep pinotdb`.)
4. **Datasets → + Dataset**. Database = `Pinot — finpulse`,
   schema = `default`, table = `transactions_scored`. This is the
   logical hybrid name from Step 9 — Superset queries it; Pinot's
   broker picks between the real-time and offline physical tables.
5. Confirm the dataset preview returns rows (run the producer briefly
   if you want fresh data).

### 10c — Build the three dashboards

Each dashboard is chosen to exercise a different part of the hybrid
table:

1. **Live fraud-rate monitor** — `count(*) FILTER (WHERE risk_score >= 2)
   / count(*)` over the last 60 min, refreshed every 10 s. Exercises
   the real-time path; the chart updates within seconds of producer
   replay.
2. **Per-rule false-positive analysis** — joins `triggered_rules` array
   against `confirmed_fraud` from the offline path, breaks down
   precision per rule. Exercises the offline path and the audit-grade
   labels that aren't available in real-time.
3. **Alert ticker** — a list view filtered to `risk_score >= 2`. Could
   alternately be a second Pinot real-time table on `fraud-alerts`,
   but reusing the existing table is one fewer thing to manage.

Export each dashboard to JSON under `docker/superset/dashboards/`
(Settings → Dashboards → ⋮ → Export) so they can be re-imported on a
fresh `make nuke && make up`.

**Concepts.**

- **Superset → Pinot via SQLAlchemy.** The connection string above
  uses the `pinotdb` SQLAlchemy driver (already wired into Superset
  via `docker/superset/requirements-local.txt`). Both
  `pinot-broker:8099` (query path) and `pinot-controller:9000`
  (cluster metadata) need to resolve from inside the Superset
  container — they do, on the `finpulse` docker network.
- **Caching trade-offs.** Superset has its own query cache. For a
  live dashboard you want it short (≤10 s) so the screen actually
  updates; for offline-heavy dashboards it can be longer.
- **Refresh interval ≠ Pinot freshness.** The dashboard refresh rate
  is how often Superset re-runs the query; Pinot's freshness is how
  fast new Kafka events become queryable. Both have to be tuned.

**Verify.**

```sh
# 1. Run a producer + Flink job + Pinot ingest:
python src/producer/replay_transactions.py --rate 200 --limit 10000

# 2. Open Superset:
open http://localhost:8088
#   - Login admin / admin
#   - Open the "Live fraud-rate monitor" dashboard
#   - Confirm the chart shows numbers (not "no data") within ~10 s

# 3. Confirm hybrid path: a chart filtered on yesterday's date hits
#    the offline table; one filtered on the last 5 min hits real-time.
#    The numbers should match a hand-rolled Pinot query on each.
```

Robinhood's "what changed" slide is the success criterion at toy
scale: dashboard p95 < 1 s, freshness ≈ 5 s, no manual refresh
required.

---

## Step 11 — Airflow orchestration

**Goal.** Two DAGs:

- `daily_batch` — chains Steps 1 → 6 plus the Pinot offline-segment
  upload from Step 9, all gated on quality checks.
- `streaming_monitor` (every 15 min) — checks Kafka consumer lag,
  Flink job liveness/checkpointing, and that `fraud-alerts` is
  producing within expected bounds.

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
- **Pinot offline-segment upload as the last batch task.** This is
  what makes the hybrid table work — every nightly run pushes a
  reconciled copy of yesterday into the offline table, and the
  broker shifts the cutover boundary forward by a day.
- **Flink monitoring is different from Spark monitoring.** Liveness
  alone isn't enough; you also want to alert if the
  `lastCheckpoint` age exceeds N minutes (means exactly-once
  guarantees are eroding) or if the side-output late-event volume
  spikes (means upstream clocks are drifting).
- **Sensors.** `ExternalTaskSensor` if you want monitor → batch
  coordination; not strictly needed for class scope.

**What to build.**

- `airflow/dags/daily_batch.py`:

  ```text
  land_dims → curate_dims (4 datasets in parallel)
            → build_enriched_fact     # Spark batch-reads Kafka 'transactions' + dims
            → build_customer_features
            → run_offline_scoring → quality_gates
            → build_pinot_offline_segments → publish_metrics
  ```

- `airflow/dags/streaming_monitor.py`: three checks every 15 min —
  - Kafka consumer-group lag on `transactions` (Flink) and
    `transactions-scored` (Pinot real-time table) via the Kafka
    admin API.
  - Flink job state + last-checkpoint age via the Flink REST API
    (`GET /jobs/<id>` and `GET /jobs/<id>/checkpoints`).
  - Alert if lag > N for X minutes, alert rate is 0 for X minutes,
    or last checkpoint is older than 5 minutes.
- Schedule: `daily_batch` at `0 2 * * *`; monitor at `*/15 * * * *`.

**Verify.**

```sh
# Trigger a manual run from the UI at http://localhost:8081
# Then on the CLI:
docker compose exec airflow-scheduler airflow dags state daily_batch <date>
# Expect: success
# Confirm offline segments showed up:
curl -fsS http://localhost:9100/segments/transactions_scored_OFFLINE | jq length
```

---

## Step 12 — Analysis notebook + business-impact report

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
| Stage 3 — Streaming | Steps 7, 8 (Flink Kafka→Kafka), 9 (Pinot real-time ingest) |
| Stage 4 — Orchestration | Step 11 |
| Real-time analytics serving | Steps 9 (Pinot hybrid table), 10 (Superset) |
| Feature engineering depth | Step 5 (creativity), Step 6 (rules), Step 12 (analysis) |
| Class-imbalance awareness | Step 6 (PR-AUC, threshold tuning), Step 12 (visuals) |
| Real-time architecture | Step 8 (event-time, watermark, broadcast state, exactly-once), Step 9 (hybrid tables) |
| Business-impact framing | Step 10 (live dashboards), Step 12 (dollars per question) |

**Rough effort estimate (assuming the project is the user's full focus):**

- Step 1, 3: ~2–3 hours (mostly mechanical: 4 dim files, one per dataset).
- Step 4: ~2–3 hours (Spark Kafka batch source is new; the join logic is
  the same as a Parquet-only job).
- Step 5: ~1–2 hours (windowed aggs).
- Step 6: ~2–4 hours (rules quick, ML optional).
- Step 7: ~2 hours.
- Step 8: ~6–10 hours (Flink + event-time + exactly-once is the
  steepest ramp; budget extra if you've never written Flink before).
- Step 9: ~3–5 hours (schema + table-config + Spark→Pinot segment job).
- Step 10: ~2–3 hours (Superset connection + 3 dashboards).
- Step 11: ~2–3 hours.
- Step 12: ~3–6 hours depending on polish.

**Total: ~30–45 hours of focused work.** The Flink + Pinot path
adds ~10 hours over the original Spark-Streaming-only plan, but
buys you the real-time OLAP serving that the Robinhood talk argues
is the actual production shape.

---

## How to use this plan

- One step per session — no skipping ahead. Each builds on the
  previous step's outputs at known HDFS / topic paths.
- At the end of each step, run **exactly** the command in its
  **Verify** block before moving on. If verification fails, debug
  out loud rather than working around the symptom.
- When introducing a concept marked in the **Concepts** list, take
  a moment to read about it (Spark docs / the per-component reference
  under `docs/infrastructure/`) before writing the code — the goal of
  this project is learning, not throughput.
