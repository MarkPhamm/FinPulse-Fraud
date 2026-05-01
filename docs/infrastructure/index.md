# Infrastructure reference

Per-component reference for every service in
[`docker-compose.yml`](../../docker-compose.yml) — image, ports,
volumes, configuration source, healthcheck, and the *why* behind each
non-obvious choice.

This directory answers **"how is component X wired?"**. For
**"what data flows through component X?"** read
[`docs/plans/dataflow.md`](../plans/dataflow.md). For **"what is the
project trying to build?"** read [`docs/scenario.md`](../scenario.md).

## Components

| Doc                          | What it covers                                                  | Compose services                                                       |
|------------------------------|-----------------------------------------------------------------|------------------------------------------------------------------------|
| [`hdfs.md`](hdfs.md)         | Distributed FS for the four dimension datasets and `/analytics` | `namenode`, `datanode-1`, `datanode-2`                                 |
| [`spark.md`](spark.md)       | Batch compute — Kafka + HDFS dim joins, feature store, scoring  | `spark-master`, `spark-worker-1`, `spark-worker-2`                     |
| [`kafka.md`](kafka.md)       | Source of truth for the `transactions` fact stream + UI         | `kafka`, `kafdrop`                                                     |
| [`airflow.md`](airflow.md)   | Orchestrator for the nightly batch + monitoring DAGs            | `postgres`, `airflow-init`, `airflow-webserver`, `airflow-scheduler`   |
| [`flink.md`](flink.md)       | Streaming scoring — event-time, exactly-once with Kafka         | `flink-jobmanager`, `flink-taskmanager`                                |
| [`pinot.md`](pinot.md)       | Real-time OLAP store — pre-aggregated streaming + offline hybrid table | `pinot-zookeeper`, `pinot-controller`, `pinot-broker`, `pinot-server`  |
| [`presto.md`](presto.md)     | DWH serving layer — granular Parquet via Hive Metastore         | `metastore-db`, `hive-metastore-init`, `hive-metastore`, `presto-coordinator` |
| [`superset.md`](superset.md) | BI / dashboards on top of Pinot **and** Presto                  | `superset-init`, `superset`                                            |

## Port map

Host → service. Mirrors the table in [`README.md`](../../README.md)
so this directory is self-contained.

| Service             | Host port | Container port  | Notes                                                            |
|---------------------|-----------|-----------------|------------------------------------------------------------------|
| HDFS NameNode UI    | 9870      | 9870            | <http://localhost:9870>                                          |
| HDFS NameNode RPC   | 9000      | 9000            | for `hdfs://namenode:9000` clients                               |
| Spark Master UI     | 8080      | 8080            | <http://localhost:8080>                                          |
| Spark Master RPC    | 7077      | 7077            | `spark://spark-master:7077`                                      |
| Kafka broker        | 9092      | 9092 (EXTERNAL) | host clients: `localhost:9092`; in-network: `kafka:9094`         |
| Kafdrop             | 9001      | 9000            | <http://localhost:9001> — moved off 9000 (HDFS RPC)              |
| Airflow web         | 8081      | 8080            | <http://localhost:8081> — `admin` / `admin`                      |
| Pinot Controller UI | 9100      | 9000            | <http://localhost:9100> — moved off 9000 (HDFS RPC)              |
| Pinot Broker        | 8099      | 8099            | Pinot SQL query endpoint (Superset + smoke check)                |
| Superset            | 8088      | 8088            | <http://localhost:8088> — `admin` / `admin`                      |
| Flink Jobmanager UI | 8082      | 8081            | <http://localhost:8082> — moved off 8081 (Airflow web)           |
| PrestoDB Coordinator| 8086      | 8080            | <http://localhost:8086> SQL + Web UI — moved off 8080 (Spark Master UI) |

Three host ports collide with default container ports and are
remapped: **Kafdrop** (9001 ← 9000) and **Pinot controller**
(9100 ← 9000) both step around the HDFS NameNode RPC port; **Flink
jobmanager UI** (8082 ← 8081) steps around the Airflow web UI. If
you ever bring up only a subset and wonder why a default URL doesn't
work, this is why.

## Profile groups

[`Makefile`](../../Makefile) ships three subset bring-up targets that
match natural component clusters. Use them when you only need part of
the stack and want to skip the ~12 GB resident-memory cost of the
full `make up`.

| Target          | Components started                       | Use when…                                              |
|-----------------|------------------------------------------|--------------------------------------------------------|
| `make up-core`  | HDFS, Spark, Kafka                       | Working on Spark batch jobs or Kafka producers         |
| `make up-stream`| Kafka, Flink                             | Working on the Flink streaming app                     |
| `make up-bi`    | Pinot, Superset, HMS + Presto            | Working on dashboards or ad-hoc DWH SQL                |
| `make up-dwh`   | Postgres-backed HMS + nothing else       | Inspecting / debugging the catalog in isolation        |
| `make up`       | Everything (adds Airflow on top of all)  | Full integration tests, `make smoke`, demos            |

Pinot needs its own ZooKeeper to come up; that's started as part of
`make up-bi` automatically. Airflow needs Postgres up first;
`make up` handles the dependency ordering. Component-level details
live in the per-file docs.

## Per-component smoke checks

Each component has a smoke target in [`Makefile`](../../Makefile)
that runs an end-to-end probe inside the running stack. They're the
first thing to try when something feels off.

```sh
make smoke-hdfs       # put / ls / cat / rm round-trip
make smoke-kafka      # create + produce + consume on an ephemeral topic
make smoke-spark      # spark-submit a job that reads HDFS
make smoke-airflow    # trigger smoke_dag and wait for success
make smoke-pinot      # /health on controller + broker, instance registration
make smoke-flink      # /overview on jobmanager + ≥ 1 taskmanager registered
make smoke-presto     # /v1/info + hive catalog + Spark<->HMS<->Presto round-trip
make smoke            # all of the above
```

The exact probes live in [`scripts/smoke.sh`](../../scripts/smoke.sh).

## Volumes and `make nuke`

Every component except Kafdrop, the Airflow init container, and the
Superset init container persists state to a named Docker volume.
`make nuke` runs `docker compose down -v` and **deletes every named
volume**, which means HDFS data, Kafka topics + offsets, Postgres
(Airflow metadata), Pinot ZK + controller + server data, the
Superset SQLite metadata DB, and Flink checkpoints + savepoints all
go away. The component docs each list which volume they own.

## See also

- [`docs/scenario.md`](../scenario.md) — what the project must deliver.
- [`docs/plans/dataflow.md`](../plans/dataflow.md) — how data moves
  between these components end to end.
- [`docs/plans/plan.md`](../plans/plan.md) — step-by-step build order.
- [`docker-compose.yml`](../../docker-compose.yml) — the source of
  truth; comments above each block explain the trade-offs called out
  in the per-component docs.
