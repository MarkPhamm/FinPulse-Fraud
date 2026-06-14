# Step 9 - Register the granular Hive tables for Presto

This step makes every dataset in `/curated/*` and `/analytics/*` queryable
from PrestoDB by registering it in the Hive Metastore. Pinot (Step 8) serves
the *pre-aggregated streaming* access pattern; Presto-on-HMS serves the
complementary *granular ad-hoc SQL* pattern - full row detail, arbitrary
joins, partition pruning - over the same HDFS Parquet.

This is the sister doc to
[`docs/plans/plan.md` Section Step 9](../plans/plan.md#step-9--register-the-granular-hive-tables-for-presto)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `jobs/warehouse/register_hms_tables.py` | Spark job: register all curated + analytics tables in HMS |
| HMS databases `curated`, `analytics` | Catalog namespaces Presto reads via the hive connector |

Tables registered:

| Table | Source path | Partitioned by |
|---|---|---|
| `curated.customer_profiles` | `/curated/customer-profiles/` | - |
| `curated.merchant_directory` | `/curated/merchant-directory/` | - |
| `curated.device_fingerprints` | `/curated/device-fingerprints/` | `device_type` |
| `curated.fraud_reports` | `/curated/fraud-reports/` | `fraud_type` |
| `analytics.transactions_enriched` | `/analytics/transactions_enriched/` | `dt` |
| `analytics.customer_features` | `/analytics/customer_features/` | - |
| `analytics.scored` | `/analytics/scored/` | `dt` |

## Approach: Spark saveAsTable, not Presto external tables

The plan sketches two ways to register tables - `saveAsTable` from Spark or
`CREATE EXTERNAL TABLE` from the Presto CLI. This step uses **`saveAsTable`**
because it is the same path the `make smoke-presto` round-trip already
proves works, and it sidesteps the SerDe / partition-discovery friction of
hand-writing external-table DDL for partitioned Parquet. `saveAsTable`:

1. writes Parquet into the Hive warehouse dir
   (`hdfs://namenode:9000/warehouse/<db>.db/<table>/`), and
2. registers the table (and its partitions) in HMS over Thrift,

in one call. These are **managed** tables, so the data is copied into the
warehouse rather than referenced in place - a deliberate trade-off for
reliability at this scale. `DROP TABLE` would remove the warehouse copy,
not the original `/curated` or `/analytics` Parquet.

## What this step does NOT do

- **No new transformation.** This is pure catalog registration - the bytes
  are the same scored/enriched/curated outputs from earlier steps.
- **No Pinot involvement.** Pinot has its own hybrid table (Step 8); this
  is the Presto path over the lake.
- **No external-table DDL.** If you wanted in-place external tables (no
  warehouse copy), you would `CREATE TABLE ... WITH (external_location=...)`
  in Presto and `CALL system.sync_partition_metadata(...)` per partitioned
  table - heavier and not needed here.

## Concepts you'll meet here

- **Catalog vs storage vs engine.** Three independent concerns: HDFS stores
  the Parquet bytes, HMS records what each table is called and where it
  lives, Presto queries it. Each is swappable.
- **The Hive bridge JAR is not pre-baked.** Any `spark-submit` that calls
  `saveAsTable` / `enableHiveSupport()` must pass
  `--packages org.apache.spark:spark-hive_2.12:3.5.3`, same posture as the
  Kafka connector.
- **Partition registration.** `saveAsTable(...).partitionBy(col)` registers
  each partition in HMS, so Presto prunes on that column (e.g.
  `WHERE dt = DATE '2025-03-14'`) without a manual `MSCK REPAIR`.

## Pre-flight

```sh
# 1. The Presto + HMS round-trip works.
make smoke-presto

# 2. The curated + analytics outputs exist in HDFS.
docker compose exec namenode hdfs dfs -ls /curated
docker compose exec namenode hdfs dfs -ls /analytics
```

## 9a - Register the tables

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-hive_2.12:3.5.3 \
    /opt/jobs/warehouse/register_hms_tables.py
```

Expect a `registered <db>.<table>: <n> rows` line per table and the
`curated` + `analytics` databases in the final `SHOW DATABASES`.

## 9b - Verify from Presto

```sh
docker compose exec presto-coordinator /opt/presto-cli --catalog hive \
    --execute 'SHOW SCHEMAS'

docker compose exec presto-coordinator /opt/presto-cli --catalog hive \
    --schema analytics --execute 'SHOW TABLES'

# Granular cross-schema join with partition pruning - the access pattern
# Pinot cannot serve (no row-level joins).
docker compose exec presto-coordinator /opt/presto-cli --catalog hive --execute "
SELECT m.category, count(*) AS txn_count
FROM analytics.transactions_enriched t
JOIN curated.merchant_directory m ON t.merchant_id = m.merchant_id
WHERE t.dt = DATE '2025-03-14'
GROUP BY m.category
ORDER BY txn_count DESC
LIMIT 5"
```

## What "passes" looks like

```text
registered curated.customer_profiles: 100000 rows
registered curated.merchant_directory: 10000 rows
registered curated.device_fingerprints: 600000 rows (partitioned by device_type)
registered curated.fraud_reports: 15000 rows (partitioned by fraud_type)
registered analytics.transactions_enriched: 3917437 rows (partitioned by dt)
registered analytics.customer_features: 100000 rows
registered analytics.scored: 1000000 rows (partitioned by dt)

-- Presto join:
"grocery","3172"
"retail","3078"
"restaurant","2710"
"gas_station","2217"
"online_marketplace","2163"
```

The same query through Pinot would either fail (no row-level join) or need
a precomputed aggregate - that is the access-pattern split between the two
serving engines, demonstrated in one query.

## Troubleshooting

- **`no viable alternative` / column not found.** Use the curated column
  names, not the enriched aliases - `curated.merchant_directory` has
  `category`, `name`, `country` (the enriched fact renames them to
  `merchant_category`, `merchant_name`, `merchant_country`).
- **`Table ... not found` in Presto.** The registration job did not run, or
  ran against a different HMS. Re-run 9a and confirm `SHOW DATABASES` lists
  `curated` and `analytics`.
- **`No factory for connector 'hive'`.** PrestoDB's connector name is
  `hive-hadoop2`, not `hive`; this is set in
  `docker/presto/etc/catalog/hive.properties` and should already be
  correct.
- **Partition filter returns nothing.** `dt` is a DATE column; filter with
  `DATE '2025-03-14'`, not the string `'2025-03-14'`.

## What's next

Step 10 brings up Superset and connects it to **both** serving layers - the
Pinot hybrid table for live, pre-aggregated dashboards and the Presto Hive
tables for granular cross-segment analysis.
