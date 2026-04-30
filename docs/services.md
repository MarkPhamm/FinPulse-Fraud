# Local Stack Services

Reference for every container in [`docker-compose.yml`](../docker-compose.yml).
Each service section covers: purpose, image, ports, volumes, configuration
source, healthcheck, and notes.

## Quick port map

| Host port | → | Container | Service           | URL / use                                     |
|-----------|---|-----------|-------------------|-----------------------------------------------|
| 9870      | → | 9870      | namenode          | <http://localhost:9870> (HDFS UI)             |
| 9000      | → | 9000      | namenode          | `hdfs://localhost:9000` (NameNode RPC)        |
| 8080      | → | 8080      | spark-master      | <http://localhost:8080> (Spark Master UI)     |
| 7077      | → | 7077      | spark-master      | `spark://localhost:7077` (Spark Master RPC)   |
| 9092      | → | 9092      | kafka             | `localhost:9092` (Kafka EXTERNAL listener)    |
| 9001      | → | 9000      | kafdrop           | <http://localhost:9001> (Kafka UI)            |
| 8081      | → | 8080      | airflow-webserver | <http://localhost:8081> — `admin` / `admin`   |

In-network hostnames (when one container talks to another):
`namenode`, `datanode-1`, `datanode-2`, `spark-master`, `kafka` (port 9094 internal),
`postgres`, `airflow-webserver`, `airflow-scheduler`, `kafdrop`.

## Startup dependency graph

```text
postgres (healthy) ──┐
                     ├──> airflow-init (one-shot db migrate + admin user)
                     │       └──> airflow-webserver, airflow-scheduler
namenode (healthy) ──┴──> datanode-1, datanode-2
                          └──> spark-master ──> spark-worker-1, spark-worker-2
kafka (healthy)  ────────> kafdrop
```

`make up` brings everything up in the right order via `depends_on` +
healthchecks. Expect 60–90 seconds before all services are healthy on a cold
start.

---

## HDFS NameNode  (`namenode`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `farberg/apache-hadoop:3.4.1`                                           |
| Hostname       | `namenode`                                                              |
| Host ports     | `9870` (UI), `9000` (RPC)                                               |
| Volume         | `hdfs-name` → `/hadoop/dfs/name`                                        |
| Healthcheck    | `curl -fsS http://localhost:9870/`                                      |

**Purpose.** Maintains the HDFS file-system namespace and block locations.
Single instance — there's no HA NameNode for local dev.

**First-boot behavior.** The container's entrypoint formats the NameNode
metadata directory (`hdfs namenode -format`) the first time the volume is
empty, using `clusterId=finpulse`. Subsequent restarts reuse the existing
metadata.

**Configuration.** Bind-mounted XML config files from
`./docker/hadoop-server/` into `/opt/hadoop/etc/hadoop/`:

- `core-site.xml` — `fs.defaultFS = hdfs://namenode:9000`
- `hdfs-site.xml` — `dfs.replication=2`, `dfs.namenode.name.dir=/hadoop/dfs/name`,
  `dfs.permissions.enabled=false`, `dfs.webhdfs.enabled=true`, etc.

The same two files are mounted into both DataNodes; properties that don't
apply to the role they're loaded by (e.g. `dfs.namenode.name.dir` on a
DataNode) are simply ignored. We do **not** rely on the
`<FILE>-SITE.XML_<key>` env-var translator because `farberg/apache-hadoop`
ships without it.

**Why community image.** Apache does not publish arm64 builds for
`apache/hadoop`; `farberg/apache-hadoop` is a community fork of the same
upstream Hadoop release built natively for both arm64 and amd64. See the
comment block in `docker-compose.yml`.

---

## HDFS DataNodes  (`datanode-1`, `datanode-2`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `farberg/apache-hadoop:3.4.1`                                           |
| Hostnames      | `datanode-1`, `datanode-2`                                              |
| Host ports     | none exposed                                                            |
| Volumes        | `hdfs-data-1`/`hdfs-data-2` → `/hadoop/dfs/data`                        |

**Purpose.** Store the actual HDFS data blocks. Two of them so that the
configured `dfs.replication=2` is genuinely exercised — the Stage 1 rubric
calls out replication > 1 for `fraud-reports` and `customer-profiles`.

**Configuration.** Same bind-mounted `core-site.xml` / `hdfs-site.xml` as
the NameNode. They register with `namenode:9000` on startup;
`dfs.namenode.datanode.registration.ip-hostname-check=false` allows the
docker DNS hostnames (`datanode-1`, `datanode-2`) to resolve cleanly.

**Notes.**

- `depends_on: namenode (healthy)` ensures DataNodes don't try to register
  before the NameNode is serving RPC.
- Block reports happen automatically every few seconds.
- Use the NameNode UI's **Datanodes** tab to verify both are live.

---

## Spark Master  (`spark-master`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/spark:3.5.3-python3`                                            |
| Hostname       | `spark-master`                                                          |
| Host ports     | `8080` (UI), `7077` (RPC)                                               |
| Volumes        | `./jobs:/opt/jobs`, `./docker/hadoop-client:/opt/hadoop-conf:ro`        |

**Purpose.** Coordinator for Spark's standalone cluster manager. Workers
register here; `spark-submit --master spark://spark-master:7077` schedules
applications onto the registered workers.

**Why we run it as `user: root` and pass an explicit command.** The official
`apache/spark` image is bare-bones — no Bitnami-style init script. We launch
the master JVM directly with `spark-class org.apache.spark.deploy.master.Master`.
Running as root sidesteps permission issues writing into bind-mounted
`/opt/jobs` and `/opt/hadoop-conf` directories.

**Configuration of HDFS access.** `HADOOP_CONF_DIR=/opt/hadoop-conf` points
Spark at the bind-mounted `core-site.xml` + `hdfs-site.xml` so PySpark jobs
can use bare `hdfs://namenode:9000/...` paths without per-job overrides.

**`SPARK_NO_DAEMONIZE=true`** keeps the master in the foreground so docker
can supervise it (otherwise it forks and the container exits immediately).

---

## Spark Workers  (`spark-worker-1`, `spark-worker-2`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/spark:3.5.3-python3`                                            |
| Hostnames      | `spark-worker-1`, `spark-worker-2`                                      |
| Host ports     | none exposed                                                            |
| Resources      | 2 cores × 2 GB each (4 cores / 4 GB total)                              |
| Volumes        | `./jobs:/opt/jobs`, `./docker/hadoop-client:/opt/hadoop-conf:ro`        |

**Purpose.** Execute Spark tasks for both batch jobs (Stage 2) and Structured
Streaming jobs (Stage 3) submitted to the master.

**Why two workers.** Structured Streaming jobs are long-lived — if we had
only one worker, a streaming job would consume its entire resource pool and
batch work would queue forever. With two, one worker can host the streaming
job indefinitely while the other runs daily batch work.

**Cores/memory tuning.** `--cores 2 --memory 2g` per worker (set on the
launch command). The Worker's web UI binds to container port `8081` but
isn't exposed to the host — inspect via Spark Master UI's "Workers" tab.

---

## Kafka  (`kafka`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/kafka:3.7.1`                                                    |
| Hostname       | `kafka`                                                                 |
| Host ports     | `9092` (EXTERNAL listener for host-side clients)                        |
| Volume         | `kafka-data` → `/var/lib/kafka/data`                                    |
| Healthcheck    | `kafka-broker-api-versions.sh --bootstrap-server kafka:9094`            |

**Purpose.** Single-broker Kafka cluster in **KRaft mode** (no ZooKeeper).
Hosts the topics defined by the project — `transactions`,
`fraud-alerts`, optionally `fraud-scores` (Stage 3).

**Listener layout.** Kafka serves three listeners on three ports:

| Listener     | Port | Used by                                              |
|--------------|------|------------------------------------------------------|
| `EXTERNAL`   | 9092 | host-machine clients (e.g. a producer run from your shell) — advertised as `localhost:9092` |
| `INTERNAL`   | 9094 | other containers in the docker network — advertised as `kafka:9094` |
| `CONTROLLER` | 9093 | KRaft controller quorum (only `kafka:9093` itself in this single-node setup) |

This split is required because a docker container that connects to Kafka
needs `kafka:9094`, but a script you run on your laptop reaches the broker
at `localhost:9092`. Both have to be in `KAFKA_ADVERTISED_LISTENERS` and
mapped in `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP`.

**Single-broker safety nets.** The compose explicitly sets:

- `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1`
- `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1`
- `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1`

Without these, internal topics like `__consumer_offsets` default to
replication-factor 3 and fail to create on a one-broker cluster.

**`user: "0:0"`.** Lets the entrypoint chown `/var/lib/kafka/data` on first
boot when the named volume is empty.

**`CLUSTER_ID`.** Not set; the entrypoint generates one on first format and
persists it in `meta.properties`. Don't change it after first boot — KRaft
will refuse to start on the existing log directory.

---

## Kafdrop  (`kafdrop`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `obsidiandynamics/kafdrop:latest`                                       |
| Hostname       | `kafdrop`                                                               |
| Host port      | `9001` → container `9000`                                               |

**Purpose.** Read-mostly web UI for Kafka — browse topics, partitions,
messages, and consumer-group offsets. Useful during Stage 3 to confirm a
producer is actually putting events on `transactions` and a streaming job
is emitting to `fraud-alerts`.

**Why `:latest`.** The pinned `4.0.1` tag is amd64-only; `:latest` is
multi-arch. Pin to a digest if you need reproducibility.

**Why host port 9001.** Default container port is 9000, which collides on
the host with HDFS NameNode RPC (also 9000). Remapped to 9001 to avoid that.

---

## Postgres  (`postgres`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `postgres:16-alpine`                                                    |
| Hostname       | `postgres`                                                              |
| Host ports     | none exposed                                                            |
| Volume         | `postgres-data` → `/var/lib/postgresql/data`                            |
| Healthcheck    | `pg_isready -U airflow`                                                 |

**Purpose.** Backing database for Airflow's metadata (DAG runs, task
instances, connections, variables). Not used by anything else — application
data lives in HDFS / Kafka.

**Credentials.** `airflow:airflow` against database `airflow`. Embedded in
the compose file because nothing outside the docker network can reach it.

---

## Airflow Init  (`airflow-init`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/airflow:2.10.4-python3.11`                                      |
| Lifecycle      | One-shot (runs and exits)                                               |

**Purpose.** Runs `airflow db migrate` and creates the `admin` user, then
exits successfully. Both `airflow-webserver` and `airflow-scheduler`
`depends_on` it with `service_completed_successfully`, so they only start
after the metadata schema exists.

Re-runs on every `make up` are no-ops once the schema is current.

---

## Airflow Webserver  (`airflow-webserver`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/airflow:2.10.4-python3.11`                                      |
| Hostname       | `airflow-webserver`                                                     |
| Host port      | `8081` → container `8080`                                               |
| Healthcheck    | `curl --fail http://localhost:8080/health`                              |

**Purpose.** UI + REST API for Airflow at <http://localhost:8081>. Default
login is `admin` / `admin`.

**Volumes shared with scheduler.**

- `./airflow/dags  → /opt/airflow/dags`  — the source of truth for DAGs
- `./airflow/logs  → /opt/airflow/logs`  — task logs (gitignored)
- `./airflow/plugins → /opt/airflow/plugins`
- `./jobs          → /opt/jobs:ro`       — PySpark scripts the DAGs `spark-submit`

---

## Airflow Scheduler  (`airflow-scheduler`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/airflow:2.10.4-python3.11`                                      |
| Hostname       | `airflow-scheduler`                                                     |

**Purpose.** Watches `./airflow/dags` for new DAGs, schedules runs, and
launches tasks via `LocalExecutor` (subprocess on the same container — fine
for class-project scale, no Celery / Redis needed).

**Executor.** `LocalExecutor` is set on both webserver and scheduler. With
`LocalExecutor`, the scheduler is the worker — it forks subprocesses for
each task. Switch to `CeleryExecutor` only if task parallelism actually
becomes a bottleneck, which is unlikely at this data scale.

---

## Airflow user / file ownership

All three Airflow containers (`airflow-init`, `airflow-webserver`,
`airflow-scheduler`) run as **`user: "50000:0"`** — the built-in `airflow`
user (UID 50000) that ships with the `apache/airflow` image.

**Why not run as the host user (`$(id -u)`)?** That's what Airflow's
official quickstart `docker-compose.yaml` does, but it depends on a
startup shim that registers the host UID into `/etc/passwd` *inside* the
container before any Airflow CLI runs. Without that shim, Airflow's CLI
calls `getpass.getuser()` → `pwd.getpwuid()` → `KeyError: 'getpwuid():
uid not found: <host UID>'`. That crashes `airflow db migrate` in the
init container, so the metadata schema is never created, and
webserver/scheduler then crash-loop with `You need to initialize the
database. Please run airflow db init.`

We chose the simpler path: pin to UID 50000, skip the entrypoint shim.

**Trade-off — host-side file ownership.** Anything Airflow *writes* into
bind-mounted host paths will be owned by UID 50000 on the host:

- `./airflow/logs/` — task logs (gitignored). To delete by hand:
  `sudo rm -rf airflow/logs/*`. `make nuke` is unaffected.
- `./airflow/dags/`, `./airflow/plugins/` — Airflow only *reads* these,
  so ownership stays as your host user.

For a class-project setting where logs are throwaway, this is the right
trade-off. If you ever need host-owned logs, replicate the official
Airflow entrypoint shim (see the `airflow-init` block in the upstream
[Airflow docker-compose quickstart](https://airflow.apache.org/docs/apache-airflow/stable/howto/docker-compose/)).

---

## Volumes (named)

| Volume          | Mounted at                          | Survives `make down` | Wiped by `make nuke` |
|-----------------|-------------------------------------|----------------------|----------------------|
| `hdfs-name`     | `namenode:/hadoop/dfs/name`         | yes                  | yes                  |
| `hdfs-data-1`   | `datanode-1:/hadoop/dfs/data`       | yes                  | yes                  |
| `hdfs-data-2`   | `datanode-2:/hadoop/dfs/data`       | yes                  | yes                  |
| `kafka-data`    | `kafka:/var/lib/kafka/data`         | yes                  | yes                  |
| `postgres-data` | `postgres:/var/lib/postgresql/data` | yes                  | yes                  |

`make nuke` runs `docker compose down -v` — every named volume is destroyed.
That deletes HDFS metadata and Airflow run history; you'll have to re-load
data into HDFS afterwards.

## Bind mounts (host paths)

| Host path                  | Mounted at (in containers)                          | Purpose                                  |
|----------------------------|-----------------------------------------------------|------------------------------------------|
| `./jobs`                   | spark / airflow: `/opt/jobs`                        | PySpark scripts (read-write from spark)  |
| `./docker/hadoop-client`   | spark workers/master: `/opt/hadoop-conf` (ro)       | `core-site.xml` + `hdfs-site.xml`        |
| `./airflow/dags`           | airflow-*: `/opt/airflow/dags`                      | DAG source files                         |
| `./airflow/logs`           | airflow-*: `/opt/airflow/logs`                      | Task logs (gitignored)                   |
| `./airflow/plugins`        | airflow-*: `/opt/airflow/plugins`                   | Custom operators / hooks                 |
