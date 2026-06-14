# Step 10 - Superset dashboards on Pinot and Presto

This step wires Superset to **both** serving layers - the Pinot hybrid
table from Step 8 (live, pre-aggregated) and the Presto Hive tables from
Step 9 (granular ad-hoc) - and builds three dashboards that each exercise a
different part of the architecture.

This is the sister doc to
[`docs/plans/plan.md` Section Step 10](../plans/plan.md#step-10--superset-dashboards-on-pinot-and-presto)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `docker/superset/register_superset.py` | Idempotent REST-API setup of both DB connections + the core datasets |
| Superset database connections | `Pinot - finpulse`, `Presto - finpulse` |
| Superset datasets | `transactions_scored` (Pinot); `transactions_enriched`, `scored`, `merchant_directory` (Presto) |
| Three dashboards (built in the UI) | Live fraud monitor, per-rule analysis, cross-segment breakdown |

## What is automated vs manual

- **Automated** (`register_superset.py`): the two database connections and
  the datasets they sit on. This is the fiddly, repeatable part, and
  creating a dataset forces Superset to introspect the table's columns -
  which only succeeds if the connection actually works, so it doubles as a
  connection test.
- **Manual** (Superset UI): chart and dashboard layout. Superset's
  chart/dashboard REST payloads are verbose and brittle to hand-author, so
  this step documents each chart's dataset + SQL and leaves the drag-and-
  drop to the UI. Export finished dashboards to
  `docker/superset/dashboards/` so they survive `make nuke`.

## Connection URIs

Both use **in-network** ports (Superset is on the `finpulse` docker
network), so the host remaps do not apply:

| Display name | SQLAlchemy URI | Driver |
|---|---|---|
| `Pinot - finpulse` | `pinot://pinot-broker:8099/query/sql?controller=http://pinot-controller:9000` | `pinotdb` |
| `Presto - finpulse` | `presto://presto-coordinator:8080/hive/default` | `pyhive[presto]` |

## Pre-flight

```sh
# 1. Superset is healthy.
curl -fsS http://localhost:8088/health      # -> OK

# 2. Both drivers are importable in the container.
docker compose exec superset python -c "import pinotdb, pyhive; print('drivers ok')"

# 3. The serving layers have data (Steps 8 and 9).
curl -fsS -X POST http://localhost:8099/query/sql -d '{"sql":"SELECT count(*) FROM transactions_scored"}'
docker compose exec presto-coordinator /opt/presto-cli --catalog hive \
    --schema analytics --execute 'SHOW TABLES'
```

## 10a - Superset is already up

Superset comes up with `make up` (`superset-init` runs once, then the
long-lived `superset` gunicorn). Login at <http://localhost:8088> with
`admin` / `admin`.

## 10b - Register connections + datasets

```sh
docker compose cp docker/superset/register_superset.py superset:/app/register_superset.py
docker compose exec superset python /app/register_superset.py
```

Expect:

```text
Registering databases:
  created database: Pinot - finpulse (id=1)
  created database: Presto - finpulse (id=2)
Registering datasets:
  created dataset: Pinot - finpulse.transactions_scored
  created dataset: Presto - finpulse.transactions_enriched
  created dataset: Presto - finpulse.scored
  created dataset: Presto - finpulse.merchant_directory
```

Re-running is safe - existing databases/datasets are skipped by name.

## 10c - Build the three dashboards (UI)

Each dashboard targets the engine that fits its access pattern.

### Dashboard 1 - Live fraud-rate monitor (Pinot)

Dataset: `transactions_scored` (Pinot). Set the dashboard auto-refresh to
10s. Charts:

```sql
-- Fraud rate, last 60 minutes (Big Number)
SELECT CAST(SUM(CASE WHEN predicted_fraud THEN 1 ELSE 0 END) AS DOUBLE)
       / count(*) AS fraud_rate
FROM transactions_scored
WHERE event_time > ago('PT60M');

-- Alert volume by action (Pie)
SELECT recommended_action, count(*)
FROM transactions_scored
GROUP BY recommended_action;
```

Pinot wins here: sub-second on a fixed schema, refreshed every few seconds.

### Dashboard 2 - Per-rule trigger analysis (Pinot)

Dataset: `transactions_scored` (Pinot). One Big Number / bar per rule:

```sql
SELECT
  SUM(CASE WHEN rule_high_amount        THEN 1 ELSE 0 END) AS high_amount,
  SUM(CASE WHEN rule_velocity           THEN 1 ELSE 0 END) AS velocity,
  SUM(CASE WHEN rule_intl_mismatch      THEN 1 ELSE 0 END) AS intl_mismatch,
  SUM(CASE WHEN rule_high_risk_merchant THEN 1 ELSE 0 END) AS high_risk_merchant
FROM transactions_scored;
```

### Dashboard 3 - Cross-segment fraud breakdown (Presto)

Dataset: `transactions_enriched` (Presto), joined to `merchant_directory`
and `customer_profiles`. This is the free-form, multi-table drill-down
Pinot cannot serve:

```sql
SELECT m.category,
       t.country,
       count(*)                                            AS txns,
       SUM(CASE WHEN t.confirmed_fraud THEN 1 ELSE 0 END)  AS frauds,
       CAST(SUM(CASE WHEN t.confirmed_fraud THEN 1 ELSE 0 END) AS DOUBLE)
           / count(*)                                      AS fraud_rate
FROM analytics.transactions_enriched t
JOIN curated.merchant_directory m ON t.merchant_id = m.merchant_id
GROUP BY m.category, t.country
ORDER BY fraud_rate DESC
LIMIT 50;
```

Export each finished dashboard (Settings -> Dashboards -> ... -> Export) to
`docker/superset/dashboards/` so they re-import on a fresh stack.

## Verify

```sh
# Connections + datasets exist (re-run the script: everything "exists").
docker compose exec superset python /app/register_superset.py

# In the UI:
#  - Open SQL Lab, pick "Pinot - finpulse", run the Dashboard 1 query.
#  - Pick "Presto - finpulse", run the Dashboard 3 join.
#  - Both should return rows; replay the producer for fresh Pinot data.
```

## Troubleshooting

- **`Connection failed` on a database.** Check the driver
  (`docker compose logs superset | grep -E 'pinotdb|pyhive'`) and that the
  URI uses in-network hosts (`pinot-broker:8099`, `presto-coordinator:8080`).
- **Dataset creation 422 / no columns.** The table is not visible to the
  engine yet - confirm Step 8 (Pinot table) / Step 9 (HMS registration)
  ran. For Pinot, the dataset has no schema; for Presto, use schema
  `analytics` or `curated`.
- **Pinot `ago()` errors.** Pinot time functions need the time column in
  the configured format; `event_time` is `SIMPLE_DATE_FORMAT` seconds.
- **Empty live charts.** Pinot only has data the Flink job produced - run
  `src/producer/replay_transactions.py` and confirm the Flink job is
  RUNNING.

## What's next

Step 11 wraps the batch pipeline in an Airflow `daily_batch` DAG (land ->
curate -> enrich -> features -> score -> Pinot offline segments) plus a
`streaming_monitor` DAG that watches Kafka lag and Flink checkpoint health.
