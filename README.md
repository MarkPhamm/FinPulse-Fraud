# FinPulse-Fraud

Fraud detection & transaction analytics on a HDFS / Spark / Kafka / Airflow stack.
See [`docs/scenario.md`](docs/scenario.md) for the project brief.

## Layout

```text
docker-compose.yml          # HDFS + Spark + Kafka + Kafdrop + Airflow + Postgres
docker/hadoop-client/       # core-site.xml + hdfs-site.xml mounted into Spark workers
Makefile                    # up / down / logs / smoke / nuke
.env.example                # optional pip add-ons for Airflow
scripts/smoke.sh            # HDFS round-trip -> Kafka produce/consume -> Spark->HDFS job -> Airflow DAG
jobs/smoke_spark.py         # PySpark word-count read from hdfs://
airflow/dags/smoke_dag.py   # Trivial DAG used by `make smoke-airflow`
data/                       # Source datasets (*.csv.gz / *.json.gz) + generate_data.py
docs/scenario.md            # Project brief
```

Per-service reference (image, ports, volumes, configuration, caveats) lives in
[`docs/services.md`](docs/services.md).

## Service map

| Service           | Host port | Container port      | Notes                                                             |
|-------------------|-----------|---------------------|-------------------------------------------------------------------|
| HDFS NameNode UI  | 9870      | 9870                | <http://localhost:9870>                                             |
| HDFS NameNode RPC | 9000      | 9000                | for `hdfs://namenode:9000` clients                                |
| Spark Master UI   | 8080      | 8080                | <http://localhost:8080>                                             |
| Spark Master RPC  | 7077      | 7077                | `spark://spark-master:7077`                                       |
| Kafka broker      | 9092      | 9092 (EXTERNAL)     | host clients: `localhost:9092`; in-network: `kafka:9094`          |
| Kafdrop           | 9001      | 9000                | <http://localhost:9001> (moved off 9000 to avoid HDFS RPC clash)    |
| Airflow web       | 8081      | 8080                | <http://localhost:8081> — `admin` / `admin`                         |

Spark runs as **1 master + 2 workers** (2 cores, 2 GB each) so a long-lived
Structured Streaming job can hold one worker's slots while batch work runs on
the other. HDFS runs as **1 NameNode + 2 DataNodes** so replication > 1 is
actually exercised.

## Prerequisites

- Docker Desktop with **≥ 12 GB RAM, 4+ CPUs** allocated. Full stack resident is ~9 GB.
- Apple Silicon and amd64 both supported (all images are multi-arch).

## First-time bring-up

```sh
make env                # one-time: copy .env.example -> .env
docker compose pull     # ~5 GB of images, one-time
make up                 # ~60-90s until everything is healthy
make smoke              # HDFS round-trip + Kafka produce/consume + Spark->HDFS job
make smoke-airflow      # trigger smoke_dag and wait for success
```

## Common targets

| Target                   | What it does                                                |
|--------------------------|-------------------------------------------------------------|
| `make up`                | Start the full stack                                        |
| `make up-core`           | HDFS + Spark + Kafka only (skip Airflow)                    |
| `make down`              | Stop containers, keep volumes                               |
| `make nuke`              | Stop **and delete** all volumes (HDFS / Kafka / Postgres)   |
| `make ps`                | Show running services                                       |
| `make logs s=<service>`  | Tail logs for one service, e.g. `make logs s=namenode`      |
| `make smoke`             | Run HDFS + Kafka + Spark smoke checks                       |
| `make smoke-airflow`     | Trigger the smoke DAG and wait for `success`                |

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
| `generate_data.py`            | 14 KB  | regenerator script |

## Caveats / known follow-ups

1. **Kafka connector for Structured Streaming** is *not* baked into the Spark
   image. Streaming jobs need
   `--packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1` on
   `spark-submit`. First run downloads the JAR; bake into a custom image later
   if startup time matters.
2. **`data/*.gz` is not gitignored** but `transactions.csv.gz` (24 MB) is past
   GitHub's recommended file size. Decide whether to commit it or rely on
   `generate_data.py` / re-download.
