# FinPulse-Fraud

Fraud detection & transaction analytics on a HDFS / Spark / Kafka / Airflow stack.
See [`docs/scenario.md`](docs/scenario.md) for the project brief.

## Layout

```text
docker-compose.yml   # HDFS + Spark + Kafka + Airflow + Pinot + Superset + Flink + HMS + PrestoDB
Makefile             # up / down / logs / smoke / nuke
plan.md              # End-to-end build plan, 10 small steps
.env.example         # Optional pip add-ons for Airflow

airflow/             # DAGs, plugins, task logs
data/                # Source datasets (gzipped CSV / JSON)
docker/              # Bind-mounted config for Hadoop, Spark, Superset, Flink, HMS, Presto
docs/                # scenario.md (brief), infrastructure/ (per-container ref), plans/ (dataflow + plan)
jobs/                # Spark jobs (batch curate / enrich on HDFS Parquet)
notebooks/           # Analysis notebooks answering the 7 business questions
scripts/             # Host-side helpers (smoke.sh, generate_data.py)
src/producer/        # Kafka producers (replays transactions.csv.gz to Kafka)
src/consumer/        # Non-Spark stream consumers (e.g., Flink fraud-scoring app)
utils/               # Standalone CLI utilities (Pinot schema loaders, ad-hoc Kafka inspectors, one-off data fixers)
```

Per-service reference (image, ports, volumes, configuration, caveats) lives
under [`docs/infrastructure/`](docs/infrastructure/index.md) — one doc per
component (HDFS, Spark, Kafka, Airflow, Pinot, **PrestoDB**, Superset, Flink).

## Service map

| Service             | Host port | Container port  | Notes                                                            |
|---------------------|-----------|-----------------|------------------------------------------------------------------|
| HDFS NameNode UI    | 9870      | 9870            | <http://localhost:9870>                                          |
| HDFS NameNode RPC   | 9000      | 9000            | for `hdfs://namenode:9000` clients                               |
| Spark Master UI     | 8080      | 8080            | <http://localhost:8080>                                          |
| Spark Master RPC    | 7077      | 7077            | `spark://spark-master:7077`                                      |
| Kafka broker        | 9092      | 9092 (EXTERNAL) | host clients: `localhost:9092`; in-network: `kafka:9094`         |
| Kafdrop             | 9001      | 9000            | <http://localhost:9001> (moved off 9000 to avoid HDFS RPC clash) |
| Airflow web         | 8081      | 8080            | <http://localhost:8081> — `admin` / `admin`                      |
| Pinot Controller UI | 9100      | 9000            | <http://localhost:9100> (moved off 9000 to avoid HDFS RPC clash) |
| Pinot Broker        | 8099      | 8099            | Pinot SQL query endpoint (used by Superset + smoke check)        |
| Superset            | 8088      | 8088            | <http://localhost:8088> — `admin` / `admin`                      |
| Flink Jobmanager UI | 8082      | 8081            | <http://localhost:8082> (moved off 8081 to avoid Airflow clash)  |
| PrestoDB Coordinator| 8086      | 8080            | <http://localhost:8086> SQL + Web UI (moved off 8080 to avoid Spark clash) |

Spark runs as **1 master + 2 workers** (2 cores, 2 GB each) so the
nightly Airflow DAG can run two batch jobs in parallel (e.g.
`build_enriched_fact` and `build_pinot_offline_segments`). Streaming
runs on Flink (1 jobmanager + 1 taskmanager, 4 task slots). HDFS runs
as **1 NameNode + 2 DataNodes** so replication > 1 is actually exercised.

The serving layer is **two engines, two roles**: Pinot for the
*pre-aggregated streaming* `transactions_scored` hybrid table
(sub-second on a fixed schema), and PrestoDB-on-HMS for *ad-hoc SQL
over granular Parquet* in HDFS (second-scale, arbitrary joins).
Superset connects to both. See
[`docs/infrastructure/presto.md`](docs/infrastructure/presto.md).

## Prerequisites

- Docker Desktop with **≥ 10 GB RAM, 4+ CPUs** allocated.
  **You may need to bump this to 12 GB** if you keep the stack up
  for more than ~1 hour: JVM heaps in Kafka / Flink / Pinot creep
  upward over time (`make smoke` adds a transient ~1 GB Spark
  surge on top), and at 10 GB the kernel will OOM-kill Presto
  mid-smoke once the resident set crosses ~9.7 GB. A fresh
  `make down && make up` resets heaps to baseline and recovers,
  but the durable fix is to give Docker more memory:
  Docker Desktop → Settings → Resources → Memory → 12 GB.
  Symptoms: `make smoke-presto` fails at the Spark→HMS→Presto
  round-trip step; `docker inspect presto-coordinator
  --format '{{.State.OOMKilled}}'` returns `true`.
- Apple Silicon and amd64 both supported (all images are multi-arch).
  PrestoDB prints *"Support for the ARM architecture is experimental"*
  on arm64 — advisory, not a failure.

## First-time bring-up

```sh
make env                # one-time: copy .env.example -> .env
make hive-deps          # one-time: download Postgres JDBC driver (~1.2 MB) for HMS
docker compose pull     # ~6 GB of images, one-time
make up                 # ~60-90s until everything is healthy
make smoke              # every smoke check (HDFS / Kafka / Spark / Airflow / Pinot / Flink / Presto)
```

## Common targets

| Target                   | What it does                                                                 |
|--------------------------|------------------------------------------------------------------------------|
| `make up`                | Start the full stack                                                         |
| `make up-core`           | HDFS + Spark + Kafka only (skip Airflow)                                     |
| `make up-bi`             | Pinot + Superset + HMS + Presto (skip everything else)                       |
| `make up-dwh`            | Just the HMS stack (Postgres + hive-metastore-init + hive-metastore)         |
| `make hive-deps`         | One-time: download Postgres JDBC driver to `docker/hive-metastore/jars/`     |
| `make down`              | Stop containers, keep volumes                                                |
| `make nuke`              | Stop **and delete** all volumes (HDFS / Kafka / Postgres / HMS / Pinot / …)  |
| `make ps`                | Show running services                                                        |
| `make logs s=<service>`  | Tail logs for one service, e.g. `make logs s=namenode`                       |
| `make smoke`             | Every smoke check: HDFS / Kafka / Spark / Airflow / Pinot / Flink / Presto    |
| `make smoke-airflow`     | Trigger the smoke DAG and wait for `success`                                  |
| `make smoke-presto`      | Spark `saveAsTable` -> HMS -> Presto SQL round-trip                           |

## Data

Source datasets live in [`data/`](data/) and were copied from
`prof-tcsmith/ism6562s26-class/final-projects/data/05-finpulse-fraud`.
All files are gzip-compressed; Spark reads `*.csv.gz` and `*.json.gz`
natively, so no manual `gunzip` is needed.

| File                          | Size  | Records   |
|-------------------------------|-------|-----------|
| `transactions.csv.gz`         | 24 MB | 1,000,000 |
| `device-fingerprints.csv.gz`  | 7.3 MB | 600,000   |
| `customer-profiles.json.gz`   | 2.8 MB | 100,000   |
| `fraud-reports.json.gz`       | 284 KB | 15,000    |
| `merchant-directory.csv.gz`   | 143 KB | 10,000    |

To regenerate the dataset from scratch (seed `2041`, deterministic):
`python3 scripts/generate_data.py` — overwrites `data/*.gz` in place.

## Caveats / known follow-ups

1. **Kafka connector** is *not* baked into the Spark image. Spark batch
   jobs that read Kafka topic `transactions` (the source of truth for
   transaction facts — see `docs/plans/dataflow.md`) need
   `--packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1` on
   `spark-submit`. First run downloads the JAR; bake into a custom image
   later if startup time matters. Flink ships its own Kafka connector
   bundled into the image.
2. **`data/*.gz` is not gitignored** but `transactions.csv.gz` (24 MB) is past
   GitHub's recommended file size. Decide whether to commit it or rely on
   `scripts/generate_data.py` / re-download.
