# Step 4 — Spark batch: build the enriched fact in `/analytics`

This step stands up `jobs/enrich/build_enriched_fact.py` — a Spark
**batch** job that consumes the entire `transactions` Kafka topic by
offset range, joins it with the four `/curated/*` Parquet dimensions
from Step 2, adds a `confirmed_fraud` label, and writes
`/analytics/transactions_enriched/` partitioned by date. After this
step we have one wide table that downstream feature engineering
(Step 5) and detection (Step 6) can read without re-doing the joins.

This is the sister doc to
[`docs/plans/plan.md` § Step 4](../plans/plan.md#step-4--build-the-master-enriched-fact-in-analytics)
— the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact                                              | Purpose                                                                |
|-------------------------------------------------------|------------------------------------------------------------------------|
| `jobs/enrich/build_enriched_fact.py`                  | Spark batch job: Kafka → join 4 dims → label → write Parquet           |
| `jobs/enrich/check_fraud_rate.py`                     | One-shot verifier: prints the global `confirmed_fraud` rate            |
| `/analytics/transactions_enriched/dt=YYYY-MM-DD/...`  | Snappy-Parquet, ~1M rows, ~30 columns, partitioned by date             |

The output schema is the **transaction columns** + **selected fields
from each dimension** + the **label**:

```text
txn_id, timestamp, card_id, merchant_id, amount, currency,
merchant_category, country, channel, is_international,
-- from customers (broadcast, INNER on card_id):
age, income_bracket, account_age_months, avg_monthly_spend,
home_country, typical_categories, credit_limit,
-- from merchants (broadcast, INNER on merchant_id):
merchant_name, merchant_country, risk_score, avg_transaction_amount,
-- from devices (LEFT on txn_id, sparse):
device_type, os, browser, ip_country, is_vpn, is_known_device,
-- from fraud_reports (LEFT on txn_id, very sparse):
fraud_type, resolution, amount_disputed,
-- derived:
confirmed_fraud, dt
```

## What this step does NOT do

- **No features yet.** No velocity windows, no per-card baselines —
  Step 5 does that. We're producing the **wide fact**, not feature
  vectors.
- **No model.** No rules engine, no ML. Step 6 reads this table and
  produces fraud scores.
- **No streaming.** The Kafka source is read in *batch mode*
  (start-offset → end-offset, then EOF). Step 7 is where streaming
  comes in, with Flink and event-time windows.
- **No Hive Metastore.** Output is plain Parquet on HDFS. Registering
  it in HMS so Presto can query it is **Step 9**, deliberately separated
  so the catalog/storage/engine split stays clean.
- **No incremental writes.** We rewrite the whole `/analytics/transactions_enriched/`
  every run (`mode("overwrite")`). The Kafka topic is the system of
  truth; downstream is rebuildable from it.

## Concepts you'll meet here

- **Spark's Kafka source in batch mode.** Same connector that powers
  Structured Streaming, but with two extra options — `startingOffsets`
  and `endingOffsets` — turning it into a one-shot read.
  - `"earliest"` / `"latest"` (string) — read the whole topic, start
    to end-at-job-start.
  - A JSON offset spec — `{"transactions":{"0":12345,"1":67890,...}}`
    — pins start/end per partition. Used by Step 7's nightly Spark
    reconciliation to read *exactly* the range Flink already
    consumed, no overlap, no gap. We don't need it yet — `earliest →
    latest` is right for the first build.

  The connector is **not pre-baked** into the Spark image (Apache's
  base image ships Kafka SQL classes but not the runtime JAR). Pass
  it at submit time:

  ```
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1
  ```

  Spark downloads it from Maven Central into `~/.ivy2/` on first run
  and caches it. Same posture as the Hive bridge JAR (see CLAUDE.md §
  *Spark batch Kafka-source job*).
- **Kafka source schema is fixed.** Whatever you put in, Spark
  surfaces it as:

  | column        | type      | notes                                   |
  |---------------|-----------|-----------------------------------------|
  | `key`         | binary    | bytes — our `card_id` UTF-8-encoded     |
  | `value`       | binary    | bytes — our JSON row UTF-8-encoded      |
  | `topic`       | string    | `"transactions"`                        |
  | `partition`   | int       | 0–5 (we have 6 partitions)              |
  | `offset`      | long      | monotonically increasing per partition  |
  | `timestamp`   | timestamp | Kafka's ingestion time (broker-assigned)|
  | `timestampType`| int      | 0 = CreateTime, 1 = LogAppendTime       |

  Notice `timestamp` here is **Kafka's broker timestamp**, not the
  txn's event time. The event-time `timestamp` field lives **inside
  the JSON `value`** — we have to parse it out, and we will.
- **`from_json(...)` with an explicit schema.** Spark can't infer a
  schema for a `binary` column. You pass a `StructType` describing
  the JSON shape, and `from_json("value_str", schema)` returns a
  struct column you can `select("t.*")` to flatten. **Inference is
  not an option here** — the binary→string→JSON path doesn't see a
  representative file, only one record at a time. For ~10 columns
  this is fine; for wider topics you'd Avro/Protobuf-encode and use
  Schema Registry, but that's overkill at our scale.
- **Broadcast joins.** When one side of a join is small enough to
  fit in each executor's memory, Spark can **ship a copy to every
  executor** and do the lookup locally — no shuffle. Threshold is
  `spark.sql.autoBroadcastJoinThreshold` (default 10 MB); you can
  also force it with `broadcast(df)` from `pyspark.sql.functions`.
  - `customer-profiles` (~3 MB Parquet) — broadcast.
  - `merchant-directory` (~200 KB Parquet) — broadcast.
  - `device-fingerprints` (~600K rows, dozens of MB) — regular
    shuffle join (well above the auto-broadcast threshold; forcing it
    would OOM workers).
  - `fraud-reports` (~15K rows, tiny) — could be broadcast, but it's
    LEFT-joined and Spark already auto-broadcasts at this size, so
    we let the optimiser handle it.

  The win: customers + merchants are looked up against *every* txn.
  Without broadcast, Spark would re-shuffle ~1M txn rows by `card_id`
  and `merchant_id` — two full network passes for what is really
  "go look this up in a dictionary."
- **LEFT vs INNER join, picked by sparsity.** Not every txn has a
  device fingerprint (sessions can be missing on stored-card
  rebills), and only ~1.8% of txns ever generate a fraud report.
  LEFT-joining `devices` and `fraud_reports` keeps every txn in the
  output; INNER-joining them would silently drop most rows. Conversely
  every txn *must* have a card and a merchant — that's a referential
  invariant of the data generator — so customer / merchant are INNER
  joins. (If a txn shows up without a matching `card_id`, that's a
  data-quality bug worth surfacing, not a row to silently drop.)
- **Label leakage warning.** `fraud-reports.timestamp` is the
  *report* time, which is always **after** the txn. Using it for a
  batch label (which is what we're doing) is correct. Using it as a
  *feature* for a streaming predictor would mean the model trains on
  information it won't have at prediction time — the classic
  label-leakage trap. The output column to leak-check on later is
  `confirmed_fraud`; everything from `fraud_reports.*` is in the
  "label, not feature" bucket.
- **Partitioning by `dt`.** `timestamp` is a `string` in the source
  ("2025-06-15 09:59:52"). Cast to timestamp, then `to_date()` into a
  new `dt` column ("2025-06-15"), and `partitionBy("dt")`. Cardinality
  is ~180 (six months of dates) — sweet spot for HDFS partition
  pruning. Steps 5 and 6 will read whole months in one go; Pinot
  offline (Step 8) builds one segment per day from this layout.

## Pre-flight

```sh
# 1. /curated has all four dim datasets from Step 2.
docker compose exec namenode hdfs dfs -ls /curated
# expect: customer-profiles, device-fingerprints, fraud-reports, merchant-directory

# 2. The transactions Kafka topic has data in it (Step 3).
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic transactions
# expect: 6 lines, one per partition, each offset > 0,
#         summing to roughly 1M (or more — see Step 3e troubleshooting).

# 3. /analytics does NOT yet exist — we create it in 4c.
docker compose exec namenode hdfs dfs -ls /analytics 2>/dev/null \
    || echo "OK: /analytics not yet"

# 4. Spark cluster is up.
docker compose ps spark-master spark-worker-1 spark-worker-2
# all three Up; master should be (healthy).

# 5. Smoke job still works (sanity that HDFS ⇄ Spark is wired).
make smoke-spark
# expect: green "OK: Spark + HDFS integration works"
```

If any of those fail, fix it first. Every sub-step below assumes this
baseline.

## Warmup — Read 5 records straight from Kafka in pyspark

Before writing a job file, open an interactive PySpark shell and read
**five** Kafka records by hand. Same idea as Step 2's pyspark
warmup, except this time the source is Kafka, not gz CSV. Seeing the
fixed `(key, value, topic, partition, offset, timestamp, timestampType)`
schema in person is worth a thousand words.

```sh
docker compose exec -it spark-master /opt/spark/bin/pyspark \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1
```

First-run takes ~30s — Spark resolves the connector + its transitive
deps from Maven Central into `~/.ivy2/` inside the master container.
Subsequent runs are instant (cached).

Inside the shell:

```python
from pyspark.sql.functions import col, from_json
from pyspark.sql.types import StructType, StructField, StringType

# 1. Batch-read every record from `transactions`.
raw = (spark.read.format("kafka")
       .option("kafka.bootstrap.servers", "kafka:9094")
       .option("subscribe", "transactions")
       .option("startingOffsets", "earliest")
       .option("endingOffsets", "latest")
       .load())

# 2. Look at the fixed Kafka-source schema.
raw.printSchema()
# expect: key (binary), value (binary), topic, partition, offset,
#         timestamp, timestampType
print("rows:", raw.count())   # ~1M (or whatever offsets sum to)

# 3. Bytes → strings → typed columns.
schema = StructType([
    StructField("txn_id",            StringType()),
    StructField("timestamp",         StringType()),
    StructField("card_id",           StringType()),
    StructField("merchant_id",       StringType()),
    StructField("amount",            StringType()),  # JSON-encoded as str
    StructField("currency",          StringType()),
    StructField("merchant_category", StringType()),
    StructField("country",           StringType()),
    StructField("channel",           StringType()),
    StructField("is_international",  StringType()),
])

parsed = (raw
    .select(from_json(col("value").cast("string"), schema).alias("t"))
    .select("t.*"))

parsed.show(5, truncate=False)
parsed.printSchema()
```

A few things worth eyeballing before you `Ctrl+D` out of the shell:

- The Kafka `timestamp` and the inside-JSON `timestamp` are
  **different things** — broker ingestion time vs. event time. We
  keep the JSON one; the Kafka one is informational.
- `amount` and `is_international` come through as **strings** because
  the producer wrote `json.dumps(row)` over a `csv.DictReader` row,
  which has no types. We'll cast in 4a, not here. (Schema Registry
  + Avro would solve this; out of scope.)
- `count()` should match the offset sum from pre-flight check #2 ±0.

Exit the shell with `Ctrl+D`. The next sub-step turns this round-trip
into a proper job file.

## 4a — Skeleton job: read Kafka, parse JSON, sanity-check

Create `jobs/enrich/build_enriched_fact.py`. Mirror the shape of
[`jobs/curate/curate_merchants.py`](../../jobs/curate/curate_merchants.py):
module docstring → constants → `main()` → `if __name__ == "__main__"`.
First iteration is the warmup, refactored: read Kafka, parse JSON,
cast types, print schema + count. No joins yet.

```python
"""Build /analytics/transactions_enriched/.

Reads the entire `transactions` Kafka topic in batch mode, parses the
JSON `value` column into typed columns, joins with the four /curated/
dimensions, attaches a confirmed_fraud label, and writes Parquet
partitioned by date.

Submit (note --packages for the Kafka connector — it is NOT pre-baked
into the Spark image):

    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \\
        /opt/jobs/enrich/build_enriched_fact.py
"""
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, BooleanType,
)

BOOTSTRAP = "kafka:9094"
TOPIC = "transactions"

# Inside-the-message JSON schema — see Step 3 producer.
TXN_SCHEMA = StructType([
    StructField("txn_id",            StringType()),
    StructField("timestamp",         StringType()),
    StructField("card_id",           StringType()),
    StructField("merchant_id",       StringType()),
    StructField("amount",            StringType()),
    StructField("currency",          StringType()),
    StructField("merchant_category", StringType()),
    StructField("country",           StringType()),
    StructField("channel",           StringType()),
    StructField("is_international",  StringType()),
])


def read_transactions(spark: SparkSession):
    """Batch-read the whole `transactions` topic and return typed rows."""
    raw = (spark.read.format("kafka")
           .option("kafka.bootstrap.servers", BOOTSTRAP)
           .option("subscribe", TOPIC)
           .option("startingOffsets", "earliest")
           .option("endingOffsets", "latest")
           .load())

    return (raw
        .select(from_json(col("value").cast("string"), TXN_SCHEMA).alias("t"))
        .select("t.*")
        # producer wrote stringly-typed fields — cast now.
        .withColumn("amount",           col("amount").cast(DoubleType()))
        .withColumn("is_international", col("is_international").cast(BooleanType()))
        .withColumn("timestamp",        col("timestamp").cast("timestamp")))


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-build-enriched-fact")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    txns = read_transactions(spark)
    print(f"  read {txns.count()} txn rows from Kafka topic '{TOPIC}'")
    txns.printSchema()
    txns.show(3, truncate=False)

    spark.stop()


if __name__ == "__main__":
    main()
```

Submit (note the `--packages` flag — without it you get
*"DataSource provider kafka could not be found"*):

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
    /opt/jobs/enrich/build_enriched_fact.py
```

Expected: the schema lists 10 columns with the types you cast (`amount`
double, `is_international` boolean, `timestamp` timestamp, the rest
string), `read N txn rows` matches the offset sum from pre-flight,
and the sample rows look like real txns. The job exits clean — no
HDFS writes yet.

Commit as `step 4a: read transactions from Kafka in Spark batch mode`.

## 4b — Add the four joins

Read the four `/curated/*` Parquet tables and join them with the
right strategy for each. The mental model: customers + merchants are
**lookups** (broadcast, INNER); devices + fraud_reports are **optional
attachments** (shuffle/auto-broadcast, LEFT).

Add to the imports:

```python
from pyspark.sql.functions import broadcast
```

Add three readers (each a Parquet read on `/curated/...`) and an
enrichment function. Pre-select only the columns we keep in the
output — narrows the shuffle for the LEFT joins and makes the output
schema obvious:

```python
CURATED = "hdfs://namenode:9000/curated"


def read_dims(spark: SparkSession):
    """Read the four curated dim tables, projecting only kept columns."""
    customers = (spark.read.parquet(f"{CURATED}/customer-profiles/")
        .select(
            "card_id", "age", "income_bracket", "account_age_months",
            "avg_monthly_spend", "home_country", "typical_categories",
            "credit_limit",
        ))

    merchants = (spark.read.parquet(f"{CURATED}/merchant-directory/")
        .select(
            "merchant_id",
            col("name").alias("merchant_name"),
            col("country").alias("merchant_country"),
            "risk_score", "avg_transaction_amount",
        ))

    devices = (spark.read.parquet(f"{CURATED}/device-fingerprints/")
        .select(
            "txn_id", "device_type", "os", "browser",
            "ip_country", "is_vpn", "is_known_device",
        ))

    fraud_reports = (spark.read.parquet(f"{CURATED}/fraud-reports/")
        .select(
            "txn_id", "fraud_type", "resolution", "amount_disputed",
        ))

    return customers, merchants, devices, fraud_reports


def enrich(txns, customers, merchants, devices, fraud_reports):
    """Join txns with the four dims. Broadcast the two small ones."""
    return (txns
        .join(broadcast(customers), on="card_id",     how="inner")
        .join(broadcast(merchants), on="merchant_id", how="inner")
        .join(devices,              on="txn_id",      how="left")
        .join(fraud_reports,        on="txn_id",      how="left"))
```

Wire them into `main()` and print a couple of sanity numbers — same
spirit as 4a, just one stage further:

```python
def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-build-enriched-fact")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    txns = read_transactions(spark)
    customers, merchants, devices, fraud_reports = read_dims(spark)

    enriched = enrich(txns, customers, merchants, devices, fraud_reports)

    print(f"  txns: {txns.count()}, enriched: {enriched.count()}")
    enriched.printSchema()
    enriched.show(3, truncate=False)

    spark.stop()
```

Re-submit the same way as 4a (the `--packages` flag still applies —
Kafka is still the txn source).

Two numbers to eyeball:

- **`txns.count() == enriched.count()`.** The two INNER joins
  (customers, merchants) are on referential keys — every txn has
  exactly one matching row, so the row count survives. If `enriched
  < txns`, you've found a data-quality issue (orphan card_id or
  merchant_id) **or** a typo in the join key.
- **The two LEFT joins fan out null-mostly.** `device_type` and
  `fraud_type` will be null on the majority of rows — that's the
  point. Confirm with
  `enriched.filter(col("fraud_type").isNotNull()).count()` — expect
  ~10K–20K, well below 1M.

Commit as `step 4b: join transactions with the four curated dims`.

## 4c — Add the label and write `/analytics/transactions_enriched/`

Two derived columns:

- **`confirmed_fraud` (boolean)** — `True` when
  `resolution == "confirmed_fraud"`, `False` otherwise. We coalesce
  the null case explicitly so the column is never `null` — a missing
  fraud report means "not confirmed fraud", not "unknown."
- **`dt` (date)** — `to_date(timestamp)`. This becomes the partition
  column.

Add to the imports:

```python
from pyspark.sql.functions import to_date, when
```

Add a `label_and_partition` function:

```python
def label_and_partition(enriched):
    """Attach confirmed_fraud + dt; drop the report columns we don't keep."""
    return (enriched
        .withColumn(
            "confirmed_fraud",
            when(col("resolution") == "confirmed_fraud", True).otherwise(False),
        )
        .withColumn("dt", to_date(col("timestamp"))))
```

And write it:

```python
ANALYTICS = "hdfs://namenode:9000/analytics/transactions_enriched/"


def write(out_df) -> None:
    (out_df.write
        .mode("overwrite")
        .partitionBy("dt")
        .parquet(ANALYTICS))
```

Wire both in:

```python
def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-build-enriched-fact")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    txns = read_transactions(spark)
    customers, merchants, devices, fraud_reports = read_dims(spark)
    enriched = enrich(txns, customers, merchants, devices, fraud_reports)
    out = label_and_partition(enriched)

    write(out)
    print(f"  wrote {out.count()} rows to {ANALYTICS}")

    spark.stop()
```

Re-submit. This run takes longer (5–15 min on a laptop, depending on
how busy the workers are) — the shuffle for the devices join + the
partitioned write are the slow parts.

Quick verification (more in **Verification** below):

```sh
docker compose exec namenode hdfs dfs -ls /analytics/transactions_enriched/ | head -8
# expect: many dt=YYYY-MM-DD sub-dirs (~180), plus _SUCCESS.

docker compose exec namenode hdfs dfs -ls \
    /analytics/transactions_enriched/dt=2025-06-15/
# expect: one or more part-*.snappy.parquet files.
```

Commit as `step 4c: label confirmed_fraud and write /analytics/transactions_enriched`.

## 4d — `check_fraud_rate.py` verifier

One small job that reads `/analytics/transactions_enriched/` and
prints the global fraud rate. This is the rubric check that ties off
the step — if it prints something between 1.5% and 2.5%, the joins
landed correctly. Below that means the LEFT joins are dropping rows;
above means the label is mis-firing.

`jobs/enrich/check_fraud_rate.py`:

```python
"""Print the global confirmed_fraud rate from /analytics/transactions_enriched/.

Step 4 rubric check — expects ~0.018 (≈1.8%, ≈18K confirmed rows out of ~1M).
Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/enrich/check_fraud_rate.py
"""
from pyspark.sql import SparkSession
from pyspark.sql.functions import avg, col, count, sum as spark_sum

ANALYTICS = "hdfs://namenode:9000/analytics/transactions_enriched/"


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-check-fraud-rate")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.parquet(ANALYTICS)
    (df.agg(
        count("*").alias("total"),
        spark_sum(col("confirmed_fraud").cast("long")).alias("fraud_rows"),
        avg(col("confirmed_fraud").cast("double")).alias("fraud_rate"),
    ).show(truncate=False))

    spark.stop()


if __name__ == "__main__":
    main()
```

No `--packages` needed — this one reads plain Parquet from HDFS, no
Kafka:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/enrich/check_fraud_rate.py
```

Expected output (numbers will vary by ±5%):

```text
+-------+-----------+--------------------+
|total  |fraud_rows |fraud_rate          |
+-------+-----------+--------------------+
|1000000|18234      |0.018234            |
+-------+-----------+--------------------+
```

Commit as `step 4d: add check_fraud_rate verifier`.

## Verification

Run all four checks. The first three are the rubric; the last is
sanity that we kept the dataflow contract.

```sh
# 1. /analytics/transactions_enriched/ exists, partitioned by dt.
docker compose exec namenode hdfs dfs -ls /analytics/transactions_enriched/ | head
# expect: dt=YYYY-MM-DD sub-dirs + _SUCCESS marker.

# 2. Row count round-trips: enriched count ≈ Kafka topic offset sum.
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server kafka:9094 --topic transactions
# sum those offsets — call it K.

docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 <<'PY' 2>/dev/null
from pyspark.sql import SparkSession
spark = SparkSession.builder.appName("count-enriched").getOrCreate()
print("enriched:",
      spark.read.parquet("hdfs://namenode:9000/analytics/transactions_enriched/").count())
PY
# expect: enriched count ≈ K (equal if the producer never re-ran; otherwise
# they differ by the duplicate count — see Step 3e troubleshooting).

# 3. Fraud rate in the expected band.
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/enrich/check_fraud_rate.py
# expect: fraud_rate between 0.015 and 0.025.

# 4. Per the dataflow SoT, transactions still must NOT be in /landing
#    or /curated — Kafka stays the only source of truth for the fact.
docker compose exec namenode hdfs dfs -ls /landing 2>/dev/null \
    | grep -i transactions && echo "FAIL" || echo "OK: no /landing/transactions/"
docker compose exec namenode hdfs dfs -ls /curated 2>/dev/null \
    | grep -i transactions && echo "FAIL" || echo "OK: no /curated/transactions/"
```

### What "passes" looks like

```text
$ hdfs dfs -ls /analytics/transactions_enriched/ | head
drwxr-xr-x  - root supergroup  /analytics/transactions_enriched/_SUCCESS
drwxr-xr-x  - root supergroup  /analytics/transactions_enriched/dt=2025-01-01
drwxr-xr-x  - root supergroup  /analytics/transactions_enriched/dt=2025-01-02
... (≈180 dirs)

$ spark-submit ... check_fraud_rate.py
+-------+-----------+----------+
|total  |fraud_rows |fraud_rate|
+-------+-----------+----------+
|1000000|18234      |0.018234  |
+-------+-----------+----------+

$ hdfs dfs -ls /curated | grep -i transactions
OK: no /curated/transactions/
```

## Troubleshooting

- **`DataSource provider kafka could not be found`.** You forgot
  `--packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1` on
  the spark-submit. The connector is not pre-baked; pass it every
  Kafka job.
- **First-run hang after the `--packages` line.** Spark is downloading
  the connector + its transitive deps from Maven Central into
  `~/.ivy2/` inside the master container. Takes ~30s on first run,
  instant after. If it actually fails (no network), the master logs
  show `unresolved dependency`.
- **`from_json` returns all-null structs.** The `value` column is
  bytes — you forgot the `.cast("string")` step. The right shape is
  `from_json(col("value").cast("string"), schema)`.
- **`enriched.count() < txns.count()` by a lot.** One of the INNER
  joins (`customers` or `merchants`) is dropping rows. Two likely
  causes: (a) the producer wrote a `card_id` or `merchant_id` that
  isn't in the dim, (b) the join column was renamed in `.select(...)`
  and the keys don't match. Diagnose with
  `txns.join(customers, "card_id", "left_anti").show(5)` — that
  prints txns whose `card_id` has no match in `customers`.
- **`enriched.count() == txns.count() * 2`** (or any other multiple).
  One of the LEFT joins is hitting a non-unique key. Most likely
  `device-fingerprints` — confirm the dim is one-row-per-`txn_id`
  with `devices.groupBy("txn_id").count().filter("count > 1").show()`.
- **Fraud rate is 0.** The `confirmed_fraud` derivation isn't seeing
  the `resolution` column. Two likely causes: (a) the LEFT join
  populated `resolution` as null and our `when` correctly produced
  `False` for all of them — meaning the fraud-reports LEFT join
  isn't matching at all (check `txn_id` types/casing). (b) The
  `when` condition is comparing against the wrong string.
- **Fraud rate is way above 5%.** The `confirmed_fraud` derivation
  is treating non-confirmed reports as fraud too. The condition must
  be `col("resolution") == "confirmed_fraud"`, not
  `col("resolution").isNotNull()`. Skimming + chargebacks land in
  `resolution = "false_alarm"` or `"chargeback_only"` — those are
  not labels.
- **The job runs but `/analytics/transactions_enriched/` is empty.**
  `mode("overwrite")` with `partitionBy` deleted the directory then
  failed silently mid-write. Look for an `OutOfMemory` or HDFS error
  in the driver logs. If memory is the issue, drop the `enriched.count()`
  print line in `main()` — `count()` materializes the join twice if
  you call it before `write()`.
- **Partitioned write left a `dt=__HIVE_DEFAULT_PARTITION__/` dir.**
  Some rows have `dt` null because their `timestamp` failed to cast.
  Inspect with
  `out.filter(col("dt").isNull()).select("timestamp").show(5)` —
  if you see anything other than ISO-style timestamps, the producer
  is shipping bad rows. Decide whether to drop them or keep the
  Hive-default bucket.

## What's next

Step 5 reads `/analytics/transactions_enriched/` and computes
per-`card_id` behavioural baselines — average amount, transaction
count, typical merchants, typical countries — and writes
`/analytics/customer_baselines/`. This is the feature store that
Step 6 (rules + simple ML) and Step 7 (Flink scoring) both consume.

Don't start Step 5 until every check in **Verification** above passes
on a fresh-eyed re-read.
