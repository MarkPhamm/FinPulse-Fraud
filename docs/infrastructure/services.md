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
| 9100      | → | 9000      | pinot-controller  | <http://localhost:9100> (Pinot Controller UI) |
| 8099      | → | 8099      | pinot-broker      | `localhost:8099` (Pinot query API)            |
| 8088      | → | 8088      | superset          | <http://localhost:8088> — `admin` / `admin`   |
| 8082      | → | 8081      | flink-jobmanager  | <http://localhost:8082> (Flink web UI)        |

In-network hostnames (when one container talks to another):
`namenode`, `datanode-1`, `datanode-2`, `spark-master`, `kafka` (port 9094 internal),
`postgres`, `airflow-webserver`, `airflow-scheduler`, `kafdrop`,
`pinot-zookeeper`, `pinot-controller`, `pinot-broker`, `pinot-server`, `superset`,
`flink-jobmanager` (REST :8081, RPC :6123), `flink-taskmanager`.

## Startup dependency graph

```text
postgres (healthy) ──┐
                     ├──> airflow-init (one-shot db migrate + admin user)
                     │       └──> airflow-webserver, airflow-scheduler
namenode (healthy) ──┴──> datanode-1, datanode-2
                          └──> spark-master ──> spark-worker-1, spark-worker-2
kafka (healthy)  ────────> kafdrop

pinot-zookeeper (healthy) ──> pinot-controller (healthy)
                                     ├──> pinot-broker
                                     └──> pinot-server

superset-init (one-shot db upgrade + admin user) ──> superset

kafka (healthy) ──────> flink-jobmanager (healthy) ──> flink-taskmanager
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

**Purpose.** Execute Spark tasks for batch jobs only (Stage 2 + Pinot
offline-segment generation). Streaming runs on Flink, not Spark — see the
Flink Jobmanager section below. Spark's role here is the **batch consumer
of Kafka topic `transactions`** (per the dataflow SoT diagram) plus the
HDFS dim joins and `/analytics/*` writes.

**Why two workers.** Two batch jobs can run concurrently — e.g. the
nightly Airflow DAG can kick off `build_enriched_fact` on one worker while
`build_pinot_offline_segments` runs on the other. With one worker, the
DAG would serialize. (Originally sized for a long-lived Spark Streaming
job, but that role moved to Flink; the two-worker shape still pays off
for parallel batch tasks.)

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
Hosts the three topics defined by the dataflow (`docs/plans/dataflow.md`):
`transactions` (single source of truth for transaction facts; consumed by
both Spark in batch mode and Flink in streaming mode), `transactions-scored`
(Flink output, fed into Pinot's real-time table), and `fraud-alerts`
(Flink output for `risk_score >= 2`, used by Superset's live alert ticker).
Configure `transactions` with `retention.ms=-1` so the batch consumer can
re-read from earliest weeks later.

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

## Pinot Zookeeper  (`pinot-zookeeper`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `zookeeper:3.9`                                                         |
| Hostname       | `pinot-zookeeper`                                                       |
| Host ports     | none exposed                                                            |
| Volumes        | `pinot-zk-data` → `/data`, `pinot-zk-datalog` → `/datalog`              |
| Healthcheck    | `echo ruok \| nc -w 2 localhost 2181 \| grep -q imok`                   |

**Purpose.** Apache Pinot uses Zookeeper for cluster state (instance registry,
table assignments, segment metadata). This is a **separate** ZK instance from
anything else — Kafka migrated to KRaft and no longer needs ZK; this ZK only
serves the Pinot quartet.

**Notes.**

- `ZOO_4LW_COMMANDS_WHITELIST=ruok,stat,conf` enables the four-letter `ruok`
  protocol used by the healthcheck. ZK 3.5+ disables 4lw by default for
  security; safe to enable in this dev setup.
- Sharing a ZK across Pinot + Kafka is technically possible but couples
  lifecycles awkwardly at this scale (e.g. nuking Kafka state would break
  Pinot's cluster registration). Worth the second container.

---

## Pinot Controller  (`pinot-controller`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apachepinot/pinot:1.2.0`                                               |
| Hostname       | `pinot-controller`                                                      |
| Host port      | `9100` → container `9000`                                               |
| Volume         | `pinot-controller-data` → `/var/pinot/controller/data` (deep store)     |
| Healthcheck    | `curl -fsS http://localhost:9000/health`                                |

**Purpose.** Cluster manager for the Pinot deployment — REST API for
schema/table definitions, segment uploads, and the Pinot Web UI. Brokers and
servers register themselves with the controller via Zookeeper at startup.

**Why host port 9100 (not 9000).** The Pinot controller defaults to container
port 9000, which on the host collides with the HDFS NameNode RPC port. Same
trick as Kafdrop (host 9001 → container 9000): we remap on the host side
and leave the container-side port alone so Pinot's internal advertised
URLs stay sensible.

**Why `apachepinot/pinot` (community-published) and not Apache.** This is the
official image published by the Apache Pinot project itself (the "apachepinot"
DockerHub org is owned by the PMC). Multi-arch (arm64 + amd64) since 1.0.

**Configuration.** All passed via `command:` flags rather than a config file —
matches the project preference for everything-in-compose. `-clusterName
finpulse` is the cluster name brokers and servers must agree on. `-dataDir`
points at the named volume that holds segment files in the local-disk deep
store.

**Local-disk deep store.** The controller stores segment files on local disk
(via the `pinot-controller-data` named volume). Pinot also supports HDFS as
the deep store (`pinot.controller.storage.factory.class.hdfs`); we haven't
wired that up yet because local disk is fine at this data scale and the HDFS
deep-store config touches multiple files. Tracked as an out-of-scope follow-up.

---

## Pinot Broker  (`pinot-broker`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apachepinot/pinot:1.2.0`                                               |
| Hostname       | `pinot-broker`                                                          |
| Host port      | `8099`                                                                  |
| Healthcheck    | `curl -fsS http://localhost:8099/health`                                |

**Purpose.** Query routing layer. SQL queries (from Superset, `curl`, or any
SQLAlchemy client) hit the broker; the broker plans the query, scatter-gathers
across servers, and returns the merged result. Brokers are stateless — adding
more is a horizontal-scaling lever for query throughput.

**Notes.**

- `-brokerHost pinot-broker` is set explicitly so the controller registers
  the broker under its docker DNS name. Without this, Pinot uses the
  container's IP, which is brittle across restarts.
- The broker is the only Pinot component external clients (Superset) ever
  talk to — the controller is admin-only, the server is in-network only.

---

## Pinot Server  (`pinot-server`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apachepinot/pinot:1.2.0`                                               |
| Hostname       | `pinot-server`                                                          |
| Host ports     | none exposed                                                            |
| Volume         | `pinot-server-data` → `/var/pinot/server/data`                          |

**Purpose.** Stores segment data and executes the per-segment query work the
broker fans out. In a real cluster you'd run many; one is enough at this scale.

**Notes.**

- `-dataDir` and `-segmentDir` split index data and segment cache under the
  same named volume. (Pinot's defaults nest them, but being explicit makes
  the volume layout obvious.)
- Heap is bumped to `-Xmx1g` (vs 512m for the controller/broker) because the
  server holds segment data resident.
- No host port. Server admin (8097) and netty (8098) ports stay in-network;
  go through the broker for queries.

---

## Superset  (`superset-init`, `superset`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `apache/superset:4.1.1`                                                 |
| Hostname       | `superset`                                                              |
| Host port      | `8088` (webserver only)                                                 |
| Volumes        | `superset-data` → `/app/superset_home`; bind mounts (see below)         |
| Healthcheck    | `curl --fail http://localhost:8088/health` (webserver only)             |

**Purpose.** BI front-end. Connects to Pinot via the `pinotdb` SQLAlchemy
driver and exposes a SQL editor + dashboard / chart builder UI.

**Two-container split.** Mirrors the `airflow-init` / `airflow-webserver`
pattern: `superset-init` is one-shot — runs `db upgrade`, creates the admin
user, and `superset init` (loads default roles + permissions); `superset`
`depends_on` it with `condition: service_completed_successfully` and only
starts after the metadata schema exists.

**Why custom init/bootstrap scripts.** `apache/superset:4.1.1` no longer
ships `/app/docker/docker-init.sh` or `/app/docker/docker-bootstrap.sh`
(those scripts only exist in the upstream source repo, not in the published
image). We bring our own minimal versions:
- `docker/superset/superset-init.sh` → bind-mounted at `/app/superset-init.sh`
  on `superset-init`. Pip-installs `requirements-local.txt`, runs the three
  init commands, exits cleanly so the long-running container can start.
- `docker/superset/superset-bootstrap.sh` → bind-mounted at
  `/app/superset-bootstrap.sh` on `superset`. Pip-installs
  `requirements-local.txt` then `exec`s the image's stock
  `/usr/bin/run-server.sh` (gunicorn). Pip layer is per-container — site-
  packages live in image fs, not a volume — so this runs every restart.

**`pinotdb` driver — bind-mount, not custom Dockerfile.** Both scripts above
look for `/app/docker/requirements-local.txt` (where the upstream entrypoint
expected it, kept for forward-compatibility) and pip-install it. Our file is
one line: `pinotdb`. Trade-off: ~30 s extra startup the first time the
container runs (pip download); avoids introducing a build step to the repo.

**Metadata DB — SQLite on a named volume.** Set in
`docker/superset/superset_config.py` to
`sqlite:////app/superset_home/superset.db`. Fine for single-user class scope
(Superset themselves recommend SQLite only for dev/eval). To upgrade:
- (a) add an init SQL script under `postgres:/docker-entrypoint-initdb.d/`
  that creates a `superset` database, then
- (b) point `SQLALCHEMY_DATABASE_URI` at `postgresql+psycopg2://superset:...@postgres/superset`.

The init script only runs on a fresh postgres volume, so an existing stack
needs a one-time `make nuke` (which also wipes Airflow state) or a manual
`psql` invocation to create the DB.

**Default credentials.** `admin` / `admin` (set via `ADMIN_*` env vars on
the init container — same pattern as Airflow).

**`user: "root"`.** The bootstrap script does `pip install` at container
start, which needs write access to the image's site-packages directory.
Matches the upstream Superset docker-compose-non-dev.yml pattern.

---

## Flink Jobmanager  (`flink-jobmanager`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `flink:1.19.1-scala_2.12-java11`                                        |
| Hostname       | `flink-jobmanager`                                                      |
| Host port      | `8082` (UI, remapped from 8081 to avoid Airflow)                        |
| In-net ports   | `8081` (REST/UI), `6123` (RPC)                                          |
| Volumes        | `flink-data` → `/opt/flink/data`; bind mounts (see below)               |
| Healthcheck    | `curl -fsS http://localhost:8081/overview`                              |

**Purpose.** Coordinates the Flink cluster. Owns job submission, scheduling,
checkpoint coordination, and the REST API/UI. One per cluster.

**Why Flink in this stack.** Streaming layer per
[`docs/odsc/robinhood_infrastructure.md`](../odsc/robinhood_infrastructure.md):
event-time native, true-streaming (not micro-batch), exactly-once via
two-phase commit with Kafka. Spark's still here for batch curate / enrich;
Flink owns the Kafka → Flink → Kafka path that Pinot's real-time table
ingests from.

**Port remap (8082 ← 8081).** Flink's default UI port collides with
Airflow's webserver (8081). Same precedent as Kafdrop (9001 ← 9000) and
Pinot Controller (9100 ← 9000): only the host-side mapping changes;
inside the container Flink still listens on 8081.

**Configuration.** No bind-mounted `flink-conf.yaml`. The image accepts a
multi-line `FLINK_PROPERTIES` env var that's appended to the default config
at boot — we use that for `jobmanager.rpc.address`, RocksDB state backend,
`execution.checkpointing.interval`, and the file:// checkpoint/savepoint
dirs (under the `flink-data` volume).

**Bind mounts.**
- `./src/consumer:/opt/flink/usrlib:ro` — drop a built jar/python module
  here and submit it via `flink run /opt/flink/usrlib/<artifact>` from
  inside the container.
- `./docker/hadoop-client:/opt/hadoop-conf:ro` — HDFS client config so
  Flink jobs can read `/analytics/customer_features/` for broadcast state.

---

## Flink Taskmanager  (`flink-taskmanager`)

| Field          | Value                                                                   |
|----------------|-------------------------------------------------------------------------|
| Image          | `flink:1.19.1-scala_2.12-java11`                                        |
| Hostname       | `flink-taskmanager`                                                     |
| Host port      | none (in-network only)                                                  |
| Volumes        | `flink-data` → `/opt/flink/data`; bind mounts (same as jobmanager)      |
| Healthcheck    | none — registration with jobmanager is the readiness signal             |

**Purpose.** Executes parallel sub-tasks (operator instances) of running
Flink jobs. Holds keyed state in RocksDB and ships checkpoints to the
shared `flink-data` volume.

**Slot count.** `taskmanager.numberOfTaskSlots: 4`. Each slot is one unit
of parallelism — for a job with `parallelism = 4`, every operator gets
spread across all 4 slots. Sized to comfortably run a velocity-window
keyed by `card_id` plus a Kafka source/sink without resource contention.

**No host port.** The taskmanager only talks to the jobmanager (RPC) and
to Kafka. Job submission, log inspection, and state management all go
through the jobmanager UI on port 8082.

**Verifying registration.** `make smoke-flink` hits the jobmanager's
`/overview` endpoint and asserts at least one taskmanager is registered.
Same shape as the Pinot smoke check.

---

## Volumes (named)

| Volume                  | Mounted at                                    | Survives `make down` | Wiped by `make nuke` |
|-------------------------|-----------------------------------------------|----------------------|----------------------|
| `hdfs-name`             | `namenode:/hadoop/dfs/name`                   | yes                  | yes                  |
| `hdfs-data-1`           | `datanode-1:/hadoop/dfs/data`                 | yes                  | yes                  |
| `hdfs-data-2`           | `datanode-2:/hadoop/dfs/data`                 | yes                  | yes                  |
| `kafka-data`            | `kafka:/var/lib/kafka/data`                   | yes                  | yes                  |
| `postgres-data`         | `postgres:/var/lib/postgresql/data`           | yes                  | yes                  |
| `pinot-zk-data`         | `pinot-zookeeper:/data`                       | yes                  | yes                  |
| `pinot-zk-datalog`      | `pinot-zookeeper:/datalog`                    | yes                  | yes                  |
| `pinot-controller-data` | `pinot-controller:/var/pinot/controller/data` | yes                  | yes                  |
| `pinot-server-data`     | `pinot-server:/var/pinot/server/data`         | yes                  | yes                  |
| `superset-data`         | `superset*:/app/superset_home`                | yes                  | yes                  |
| `flink-data`            | `flink-*:/opt/flink/data`                     | yes                  | yes                  |

`make nuke` runs `docker compose down -v` — every named volume is destroyed.
That deletes HDFS metadata and Airflow run history; you'll have to re-load
data into HDFS afterwards.

## Bind mounts (host paths)

| Host path                                  | Mounted at (in containers)                            | Purpose                                  |
|--------------------------------------------|-------------------------------------------------------|------------------------------------------|
| `./jobs`                                   | spark / airflow: `/opt/jobs`                          | PySpark scripts (read-write from spark)  |
| `./docker/hadoop-client`                   | spark workers/master: `/opt/hadoop-conf` (ro)         | `core-site.xml` + `hdfs-site.xml`        |
| `./airflow/dags`                           | airflow-*: `/opt/airflow/dags`                        | DAG source files                         |
| `./airflow/logs`                           | airflow-*: `/opt/airflow/logs`                        | Task logs (gitignored)                   |
| `./airflow/plugins`                        | airflow-*: `/opt/airflow/plugins`                     | Custom operators / hooks                 |
| `./docker/superset/requirements-local.txt` | superset*: `/app/docker/requirements-local.txt` (ro)  | Extra pip deps (e.g. `pinotdb`)          |
| `./docker/superset/superset_config.py`     | superset*: `/app/pythonpath/superset_config.py` (ro)  | Metadata DB URI, secret key, flags       |
| `./docker/superset/superset-init.sh`       | superset-init: `/app/superset-init.sh` (ro)           | One-shot init (db upgrade + admin)       |
| `./docker/superset/superset-bootstrap.sh`  | superset: `/app/superset-bootstrap.sh` (ro)           | Long-running entrypoint wrapper          |
| `./src/consumer`                           | flink-*: `/opt/flink/usrlib` (ro)                     | Flink job artifacts (jars / python)      |
| `./docker/hadoop-client`                   | flink-*: `/opt/hadoop-conf` (ro)                      | HDFS client config (broadcast state)     |
