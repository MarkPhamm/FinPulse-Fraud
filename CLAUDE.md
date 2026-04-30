# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to work in this repo

**The goal is learning, not shipping speed.** Optimize your responses
accordingly:

- **Make small incremental changes, one at a time.** If a task naturally
  splits into steps (swap image → add config → verify), do them as separate
  turns rather than one giant patch. Let the user watch each step land.
- **Explain things clearly, including the *why*.** Before a non-trivial
  edit, say in plain language what you're about to change and why.
  When introducing a concept the user may not have seen yet (KRaft
  listeners, Spark deploy modes, log4j vs log4j2, HDFS config files,
  Hadoop native libs, etc.), teach the mechanism briefly — don't just
  paste a fix.
- **When something fails, diagnose out loud.** Walk through what the
  symptom means before applying a fix. The session log so far has
  multiple examples of this (the NameNode `fs.defaultFS=file:///`
  diagnosis, the `bitnami` Docker Hub deprecation, the arm64
  `NativeCodeLoader` warning).
- **Don't refactor or "improve" things that weren't asked for.** If you
  notice something worth changing, mention it as a follow-up the user
  can decide on; don't silently bundle it into the current task.
- **End each change with a short verifiable summary** — what was changed,
  what to run/look at to confirm.

## What this project is

A class-project fraud-detection / transaction-analytics build on a HDFS / Spark
/ Kafka / Airflow stack. The full assignment brief is in
[`docs/scenario.md`](docs/scenario.md) and is the **source of truth for what
needs to be built** — read it before designing anything new. The scenario
defines four required stages:

1. **Stage 1 — Data Lake (HDFS).** Landing / curated / analytics zones, Parquet
   conversion, partitioning, replication.
2. **Stage 2 — Batch (Spark).** Joins across the five datasets, aggregations,
   derived feature columns, fraud labelling.
3. **Stage 3 — Streaming (Kafka + Spark Structured Streaming).** Real-time
   transaction scoring, velocity-attack detection, fraud-alert events.
4. **Stage 4 — Orchestration (Airflow).** Daily batch DAG, quality gates,
   monitoring DAG for the streaming pipeline.

Source datasets are already in `data/` (gzipped CSV/JSON, ~34 MB total).
The data generator that produced them lives at `src/generate_data.py`.

## Where each stage lives in the repo

| Stage | Code lives in           | Notes                                                                 |
|-------|-------------------------|-----------------------------------------------------------------------|
| 1     | `jobs/` (loader scripts), `airflow/dags/` (load DAG) | HDFS zones are paths inside HDFS, not host dirs.       |
| 2     | `jobs/`                 | Run via `spark-submit` against `spark://spark-master:7077`.           |
| 3     | `producers/` (Kafka feeder), `jobs/` (streaming job) | Replay `transactions.csv.gz` to topic `transactions`. |
| 4     | `airflow/dags/`         | DAGs auto-load from this dir; logs land in `airflow/logs/` (gitignored). |

Currently scaffolded: `airflow/dags/smoke_dag.py` and `jobs/smoke_spark.py`
exist purely to prove the stack works (`make smoke`). All four stages still
need real implementations.

## Common commands

Local stack (full reference: [`Makefile`](Makefile), per-service detail in
[`docs/services.md`](docs/services.md)):

```sh
make env                # one-time: writes .env with $(id -u) for AIRFLOW_UID
make up                 # start full stack (~60-90s to healthy)
make up-core            # HDFS + Spark + Kafka only (skip Airflow)
make smoke              # HDFS round-trip + Kafka produce/consume + Spark->HDFS job
make smoke-airflow      # trigger smoke_dag and wait for success
make logs s=namenode    # tail logs for one service
make nuke               # destroys all named volumes — wipes HDFS / Kafka / Postgres data
```

Submit a Spark batch job:

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/jobs/<your_job>.py
```

Submit a Spark **Structured Streaming** job (Kafka connector is not pre-baked):

```sh
docker compose exec spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \
    /opt/jobs/<streaming_job>.py
```

HDFS one-liners (run against the NameNode container):

```sh
docker compose exec namenode hdfs dfs -ls /
docker compose exec namenode hdfs dfs -mkdir -p /landing/transactions
docker compose exec namenode hdfs dfs -put /tmp/foo.csv /landing/transactions/
```

Kafka one-liners (use the in-network listener `kafka:9094`, not `localhost:9092`):

```sh
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 --list
```

## Architecture decisions worth knowing

These are non-obvious and explain why the compose file looks the way it does.

**Spark cluster runs both batch and streaming.** Structured Streaming is just a
long-lived Spark job. There are **2 workers** (not 1) so a streaming job can
hold one worker's slots while batch runs on the other. Don't reduce to one
worker without thinking about that.

**HDFS config is bind-mounted XML, not env-var-translated.** The
`apache/hadoop` image has a `<FILE>-SITE.XML_<key>=<value>` env-var translator
that writes XML config at startup. The `farberg/apache-hadoop` arm64 fork we
use **does not** ship that translator. So config lives in
`docker/hadoop-server/{core-site,hdfs-site}.xml` (mounted into NN + both DNs)
and `docker/hadoop-client/{core-site,hdfs-site}.xml` (mounted into Spark
master + workers via `HADOOP_CONF_DIR=/opt/hadoop-conf`). When you change
HDFS config, edit the **server** files for cluster-wide settings and restart
the affected services.

**Why `farberg/apache-hadoop:3.4.1` instead of `apache/hadoop:3.3.6`.** Apache
does not publish arm64 builds for any `apache/hadoop` tag. The farberg fork
rebuilds the same upstream Hadoop release for arm64 + amd64. Trade-off
documented in the comment block at the top of the HDFS section in
`docker-compose.yml`. If Apache ever ships arm64, swap back.

**Kafka has three listeners, not one.** Topology:

| Listener   | Port | Used by                                                 |
|------------|------|---------------------------------------------------------|
| EXTERNAL   | 9092 | Host-machine clients — advertised as `localhost:9092`   |
| INTERNAL   | 9094 | Other containers in the docker network — `kafka:9094`   |
| CONTROLLER | 9093 | KRaft controller quorum                                 |

A producer running on your laptop connects to `localhost:9092`; a Spark
worker container connects to `kafka:9094`. Get this wrong and you'll see
"connection refused" or "advertised listener loop" errors.

**Single-broker Kafka requires explicit `*_REPLICATION_FACTOR=1` env vars.**
Internal topics like `__consumer_offsets` default to RF=3 and refuse to
create on a one-broker cluster. The compose file sets these explicitly —
don't remove them.

**Airflow runs `LocalExecutor`, not Celery.** No Redis, no separate worker
container. The scheduler forks subprocesses for tasks. Fine at this data
scale; revisit only if task parallelism actually becomes a bottleneck.

**Native-Hadoop WARN is suppressed by design.** `arm64` Hadoop has no
`libhadoop.so`, so JVMs log `Unable to load native-hadoop library` on every
start and fall back to pure-Java implementations. Functionally harmless. The
warning is silenced in `docker/hadoop-server/log4j.properties` and
`docker/spark/log4j2.properties`. If you're debugging a native-codec issue,
re-enable temporarily by commenting out the `NativeCodeLoader` lines.

## When to read which doc

- **`docs/scenario.md`** — what the project must deliver (rubric, business
  questions, stage requirements). Read first when adding a new feature.
- **`docs/services.md`** — per-container reference (image, ports, volumes,
  configuration source, healthcheck, "why" notes). Read when changing
  infrastructure.
- **`README.md`** — first-time bring-up, port map, data manifest, caveats.
  Read when onboarding.
- **`docker-compose.yml`** — comments above each block explain image-choice
  trade-offs (Hadoop community fork, Kafdrop tag, etc.).

## Things that are deliberately NOT set up

Skip these unless the user asks for them — they're outside scope:

- No CI / GitHub Actions
- No Python virtual env at the repo root (PySpark runs inside containers)
- No test framework (`make smoke` is the only end-to-end check)
- No linter / formatter config
- No production-grade Spark connector pre-bake — streaming jobs pass
  `--packages` at submit time
