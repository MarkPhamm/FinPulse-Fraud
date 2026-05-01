# Pinot

Apache Pinot is a real-time OLAP store. In FinPulse it backs the
**`transactions_scored` hybrid table** — a single logical table
fronted by a *real-time* path that ingests directly from Kafka
topic `transactions-scored`, plus a *nightly offline* path of
Spark-built segments rebuilt from `/analytics/transactions_enriched`.
Queries hit the Pinot broker; the broker picks offline segments for
yesterday-and-earlier and real-time segments for today,
transparently. This is the [Robinhood
pattern](../odsc/robinhood_infrastructure.md) — see
[`docs/plans/dataflow.md`](../plans/dataflow.md) for the full data
flow.

Pinot serves the **pre-aggregated streaming** access pattern;
[`presto.md`](presto.md) covers the complementary **granular
ad-hoc** access pattern over the same HDFS data lake. Two engines,
two roles.

Pinot is also why we ship a **dedicated ZooKeeper** in this stack.

## Topology

| Service            | Image                       | Hostname           | Host → container ports | What other services talk to it                                |
|--------------------|-----------------------------|--------------------|------------------------|---------------------------------------------------------------|
| `pinot-zookeeper`  | `zookeeper:3.9`             | `pinot-zookeeper`  | (no host ports)        | Pinot controller / broker / server (cluster state)            |
| `pinot-controller` | `apachepinot/pinot:1.2.0`   | `pinot-controller` | `9100→9000`            | Operators (UI / REST API), broker + server (registration), Spark (offline-segment upload) |
| `pinot-broker`     | `apachepinot/pinot:1.2.0`   | `pinot-broker`     | `8099→8099`            | Superset, smoke check, ad-hoc SQL clients                     |
| `pinot-server`     | `apachepinot/pinot:1.2.0`   | `pinot-server`     | (no host ports)        | Broker (query routing), controller (segment lifecycle)        |

Standard 3-component Pinot layout (controller / broker / server).
The broker is the **only externally-reachable** query component —
the server's :8097 (admin) and :8098 (query) are deliberately
in-network only. The controller exposes its UI on host :9100 for
table-config / segment debugging.

## Configuration source

Image entrypoint is `bin/pinot-admin.sh`; the `command:` array in
[`docker-compose.yml`](../../docker-compose.yml) provides the
subcommand (`StartController` / `StartBroker` / `StartServer`) plus
its CLI flags. There is **no** Pinot config file mounted — every
runtime parameter is on the command line.

Key flags (per-component):

| Component  | Flags                                                                                                              |
|------------|--------------------------------------------------------------------------------------------------------------------|
| Controller | `-zkAddress pinot-zookeeper:2181 -clusterName finpulse -controllerHost pinot-controller -controllerPort 9000 -dataDir /var/pinot/controller/data` |
| Broker     | `-zkAddress pinot-zookeeper:2181 -clusterName finpulse -brokerHost pinot-broker -brokerPort 8099`                   |
| Server     | `-zkAddress pinot-zookeeper:2181 -clusterName finpulse -serverHost pinot-server -serverPort 8098 -serverAdminPort 8097 -dataDir /var/pinot/server/data/index -segmentDir /var/pinot/server/data/segment` |

Every component points at the same `pinot-zookeeper:2181` and the
same `clusterName=finpulse`, which is how they find each other.
`-*Host` flags must match the docker hostname (not `localhost`) or
the broker/server self-register under unreachable addresses and
queries fail with *no segments found*.

`JAVA_OPTS` heap caps:

| Component          | `JAVA_OPTS`                              |
|--------------------|------------------------------------------|
| Controller, Broker | `-Xms256m -Xmx512m -XX:+UseG1GC`         |
| Server             | `-Xms512m -Xmx1g -XX:+UseG1GC`           |

Without these caps, each Pinot JVM's default heap would push the
four-container set (+ ZK) past 4 GB resident, leaving little for
HDFS/Spark/Kafka/Airflow on a 16 GB Docker allocation. The server
gets more headroom because it actually holds segment data and runs
queries.

ZooKeeper config: only `ZOO_4LW_COMMANDS_WHITELIST=ruok,stat,conf`
is set — needed because ZK 3.5+ restricts the four-letter-word
admin commands by default and the healthcheck below uses `ruok`.

## Volumes

| Volume                   | Mount path                          | What's persisted                          | Wiped by `make nuke` |
|--------------------------|-------------------------------------|-------------------------------------------|----------------------|
| `pinot-zk-data`          | `/data`                             | ZK snapshots                              | yes                  |
| `pinot-zk-datalog`       | `/datalog`                          | ZK transaction log                        | yes                  |
| `pinot-controller-data`  | `/var/pinot/controller/data`        | Deep-store segments (uploaded snapshots)  | yes                  |
| `pinot-server-data`      | `/var/pinot/server/data`            | Live + sealed segments, segment index     | yes                  |

After `make nuke`, the cluster comes back empty — table configs,
schemas, and ingested segments all need to be re-applied (this is
the same posture as Kafka topic state).

## Healthcheck

ZooKeeper:

```yaml
test: echo ruok | nc -w 2 localhost 2181 | grep -q imok
interval: 10s
timeout:  5s
retries:  12
```

Controller:

```yaml
test: curl -fsS http://localhost:9000/health | grep -q OK
interval: 10s
timeout:  5s
retries:  18
start_period: 30s
```

Broker:

```yaml
test: curl -fsS http://localhost:8099/health | grep -q OK
interval: 10s
timeout:  5s
retries:  18
start_period: 30s
```

Server has no healthcheck; the smoke probe queries the controller's
`/instances` endpoint to confirm both broker and server have
self-registered (`Broker_pinot-broker_8099` and
`Server_pinot-server_8098`). That catches the most common
misconfig — a hostname/port mismatch in `-*Host` / `-*Port` flags.

The longer `start_period: 30s` matters: Pinot JVMs take noticeably
longer to come up than HDFS or Kafka, and a tighter window leaks
false-negative health reports during the ramp.

## Why this shape

- **Dedicated ZK, not Kafka's.** Kafka is on KRaft — it has no ZK.
  Sharing a ZK across Pinot + Kafka is possible historically but
  couples lifecycles awkwardly: a Kafka ZK upgrade would risk
  Pinot uptime. Cheaper to ship two independent ZK pools.
- **Controller port remap (host 9100 ← container 9000).** Pinot
  defaults to :9000 for the controller UI, which collides with the
  HDFS NameNode RPC port. Same trick as Kafdrop (9001 ← 9000) —
  pick a host port out of conflict, leave the container port at
  Pinot's default.
- **Broker is the only external surface.** External clients
  (Superset, ad-hoc SQL) hit the broker; the server is not
  meant to be queried directly. Don't add a host port for the
  server unless you're debugging.
- **Three components, not all-in-one.** The `apachepinot/pinot`
  image supports a `QuickStart` mode that fuses controller/broker/
  server into one process. We split them so failure isolation is
  realistic and so the smoke check can verify each registration
  step independently.
- **Multi-arch since 1.2.0.** `apachepinot/pinot:1.2.0` ships
  arm64 and amd64. Earlier tags were amd64-only and forced
  emulation on Apple Silicon.
- **Server gets more heap.** Per the table above — controllers and
  brokers don't materialize segment data, so 512 MB is fine.
  Servers do, so they get 1 GB.

## Alternatives

Where Pinot sits in the OLAP / real-time analytics landscape:

| System                       | Lineage                          | Pick instead when…                                                                                       |
|------------------------------|----------------------------------|----------------------------------------------------------------------------------------------------------|
| **Apache Druid**             | Closest peer (Metamarkets, 2011) | Almost interchangeable for this workload. Druid has stronger time-series ergonomics and a longer track record; Pinot has better star-schema joins (since 1.0) and faster real-time ingestion of high-cardinality data. Robinhood picked Pinot — that's why we did. |
| **ClickHouse**               | Open-source columnar DB          | Single-node performance is exceptional. Different model — regular DB tables, not segment-based with a hybrid real-time/offline split. Real-time Kafka ingestion is younger.                                  |
| **Apache Doris / StarRocks** | Newer OLAP engines               | MySQL-protocol compatible, growing fast, similar real-time + batch shape. Worth evaluating for greenfield. |
| **Trino / PrestoDB + Parquet on HDFS** | Federated query        | Read-only ad-hoc analytics over a data lake. No real-time path; query latency is seconds-to-minutes. **In-stack as PrestoDB — see [`presto.md`](presto.md).** |
| **Snowflake / BigQuery**     | Cloud warehouses                 | Batch-oriented historical analytics. No sub-second freshness; cost model is pay-per-query.              |
| **Apache Kylin**             | OLAP cube precomputation         | You want aggressive precompute (cube model). Less flexible queries, more storage cost.                  |
| **DuckDB**                   | Embedded analytical DB           | Tiny scale, single-process, notebook analytics. Not a real-time multi-user serving layer.               |

**Pinot vs Druid is the real choice here** — both fit the
Robinhood pattern of *real-time table ingesting from Kafka + offline
table reconciled from a warehouse, fronted by a hybrid broker*.
Druid is the older, more mature option; Pinot has been catching up
fast on joins and SQL ergonomics. ClickHouse is the most credible
challenger in the broader category but has a different mental model.

**Pinot vs Presto is *not* the real choice — they coexist.** Pinot
answers "what is the click-through rate by network in the last 5
minutes?" in sub-second time on rolled-up segments. Presto answers
"show me every transaction over $10K from device fingerprint X
during March" by scanning raw Parquet. Same data lake, two access
patterns. See [`presto.md`](presto.md) for the granular path.

## Hybrid table conventions

Per [`docs/plans/dataflow.md`](../plans/dataflow.md), the
`transactions_scored` table is **hybrid**:

- Real-time path: Pinot servers tail Kafka topic
  `transactions-scored` directly (no Spark/Flink in the middle).
  Each consuming segment builds an in-memory row store; once it
  hits the size threshold, it's sealed into a columnar segment on
  disk and uploaded to deep store.
- Offline path: nightly Spark builds segments from
  `/analytics/transactions_enriched/dt=...` and uploads via the
  controller's REST API.
- Broker chooses which path serves any given time slice using the
  `timeBoundary` set on the table config.

Schemas and table configs live under
[`utils/pinot/`](../../utils/pinot/) (planned per the plan; today
that directory is empty — populated when Step 9 lands).

## Common commands

UIs:

- Controller: <http://localhost:9100>
- Broker (no UI; SQL endpoint at `/query/sql`):
  `curl http://localhost:8099/query/sql -d '{"sql":"select count(*) from transactions_scored"}'`

REST API:

```sh
# List instances registered in the cluster
docker compose exec pinot-controller curl -fsS \
    http://pinot-controller:9000/instances

# List tables
docker compose exec pinot-controller curl -fsS \
    http://pinot-controller:9000/tables

# Apply a schema (planned — utils/pinot/transactions_scored.schema.json)
docker compose exec pinot-controller bin/pinot-admin.sh \
    AddSchema -schemaFile /path/to/schema.json -exec
```

```sh
make smoke-pinot   # /health on controller + broker; broker + server registered
```

## Caveats

- **Heap caps are tight.** If you load a multi-GB segment at once,
  the server may OOM. Slice big offline rebuilds into per-day
  segments (the Spark builder already does this).
- **Real-time consumer lag is invisible from the broker.** Use the
  controller UI's *Tables → transactions_scored → Segments* view
  to see the consuming segment's offset position vs the Kafka
  high-water-mark.
- **Server is single-replica.** With one server, segment loss =
  data loss for whatever it held. The deep-store copies on
  `pinot-controller-data` are the recovery path; don't `make
  nuke` in the middle of a long backfill.
- **Pinot does not enforce schema on ingestion.** A type mismatch
  between the Kafka producer's JSON and the table schema will be
  silently coerced or null'd. Validate the producer's payload
  shape against `utils/pinot/transactions_scored.schema.json`
  before debugging missing rows.
