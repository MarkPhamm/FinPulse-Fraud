# Flink

Apache Flink is the **streaming** engine for the Kafka → Flink →
Kafka layer. Per [`docs/plans/dataflow.md`](../plans/dataflow.md),
Flink continuously consumes Kafka topic `transactions`, looks each
event up against broadcast state from `/analytics/customer_features`
(rebuilt nightly by Spark), applies rules + an event-time velocity
window, and writes results to two Kafka topics:
`transactions-scored` (every event — feeds Pinot's real-time table)
and `fraud-alerts` (risk ≥ 2 — feeds the Superset alert ticker).

Flink is in this stack instead of **Spark Structured Streaming**
because we want event-time native windowing, true streaming (not
micro-batch), and exactly-once via two-phase commit with Kafka.
The full rationale is in
[`docs/odsc/robinhood_infrastructure.md`](../odsc/robinhood_infrastructure.md).

## Topology

| Service             | Image                                | Hostname            | Host → container ports | What other services talk to it                              |
|---------------------|--------------------------------------|---------------------|------------------------|-------------------------------------------------------------|
| `flink-jobmanager`  | `flink:1.19.1-scala_2.12-java11`     | `flink-jobmanager`  | `8082→8081`            | Browser (UI), `flink run` clients, taskmanager (RPC)        |
| `flink-taskmanager` | `flink:1.19.1-scala_2.12-java11`     | `flink-taskmanager` | (no host ports)        | Jobmanager only                                             |

1 jobmanager + 1 taskmanager. The taskmanager exposes **4 task
slots**, so a `keyBy(card_id)` velocity window can run with
parallelism > 1 without back-pressuring the Kafka source.

UI: <http://localhost:8082> (remapped because :8081 is Airflow's).

## Configuration source

Both containers consume a multi-line `FLINK_PROPERTIES` env var
that the image entrypoint appends to `flink-conf.yaml` on startup.
Two distinct property sets — one per role — set in
[`docker-compose.yml`](../../docker-compose.yml):

**`flink-jobmanager`:**

| Property                              | Value                                       | Why                                                              |
|---------------------------------------|---------------------------------------------|------------------------------------------------------------------|
| `jobmanager.rpc.address`              | `flink-jobmanager`                          | Must resolve from taskmanager containers — docker hostname, not `localhost` |
| `rest.address`                        | `flink-jobmanager`                          | Where the REST API self-advertises                               |
| `rest.bind-address`                   | `0.0.0.0`                                   | Listen on all interfaces inside the container                    |
| `execution.checkpointing.interval`    | `30000` (30s)                               | Frequent enough to limit replay on restart, infrequent enough to not thrash RocksDB |
| `state.backend`                       | `rocksdb`                                   | Required for stateful jobs > a few MB; spills to disk            |
| `state.checkpoints.dir`               | `file:///opt/flink/data/checkpoints`        | Lives on the `flink-data` named volume                           |
| `state.savepoints.dir`                | `file:///opt/flink/data/savepoints`         | Manual snapshots for upgrades                                    |

**`flink-taskmanager`:**

| Property                              | Value                                       | Why                                                              |
|---------------------------------------|---------------------------------------------|------------------------------------------------------------------|
| `jobmanager.rpc.address`              | `flink-jobmanager`                          | Where to register on startup                                     |
| `taskmanager.host`                    | `flink-taskmanager`                         | What hostname this TM advertises back to the JM                  |
| `taskmanager.numberOfTaskSlots`       | `4`                                         | Lets a keyed parallel job actually parallelize                   |
| `state.backend`                       | `rocksdb`                                   | Same as JM — must match                                          |
| `state.checkpoints.dir`               | same as JM                                  | Same `flink-data` volume so JM + TM see the same files           |
| `state.savepoints.dir`                | same as JM                                  | Same                                                             |

Bind mounts (both containers):

| Host path           | Container path        | Purpose                                                       |
|---------------------|-----------------------|---------------------------------------------------------------|
| `flink-data` volume | `/opt/flink/data`     | Checkpoints + savepoints (must be shared between JM and TM)   |
| `./src/consumer`    | `/opt/flink/usrlib:ro`| Job artifacts — `flink run -d /opt/flink/usrlib/<job>.{jar,py}` |
| `./docker/hadoop-client` | `/opt/hadoop-conf:ro` | HDFS client config so the broadcast-state reader can fetch `/analytics/customer_features` |

## Volumes

| Volume       | Mount path           | What's persisted                              | Wiped by `make nuke` |
|--------------|----------------------|-----------------------------------------------|----------------------|
| `flink-data` | `/opt/flink/data`    | Checkpoint + savepoint state (RocksDB-backed) | yes                  |

After `make nuke`, on next bring-up Flink starts with no
checkpoints, so any submitted job replays from the Kafka source
offsets configured in its source operator (typically
`startingOffsets=latest` for production, `earliest` for replay).

## Healthcheck

Jobmanager:

```yaml
test: curl -fsS http://localhost:8081/overview > /dev/null
interval: 15s
timeout:  5s
retries:  5
start_period: 30s
```

Taskmanager has no healthcheck — registration with the jobmanager
is the integration probe. The smoke check
(`make smoke-flink`) hits the jobmanager's `/overview` from inside
the JM container and asserts `taskmanagers >= 1`. That catches the
most common misconfig — a hostname mismatch in
`jobmanager.rpc.address` / `taskmanager.host`.

`start_period: 30s` matters because Flink's JVM cold start is on
the slower side. Tighter windows produce false negatives during
ramp-up.

## Why this shape

- **Flink, not Spark Structured Streaming.** Three reasons,
  cribbed from the dataflow plan:
  1. **Event-time native** — windows operate on the txn timestamp,
     not wall-clock arrival. Late events go to a side output for
     offline correction in Spark.
  2. **Exactly-once via two-phase commit with Kafka** — both the
     `transactions-scored` write and the `fraud-alerts` write
     participate in the same Flink checkpoint, so the two topics
     never disagree about whether a given txn was seen.
  3. **Sub-second latency** — true streaming, not micro-batches.
- **1 JM + 1 TM.** Class scale; one taskmanager with 4 slots
  is plenty for a 200-msg/s replay. Adding more is mechanical
  (duplicate the `flink-taskmanager` block with a unique hostname).
- **Port remap (host 8082 ← container 8081).** Container :8081
  collides with Airflow's webserver UI. Same precedent as Pinot
  controller (9100 ← 9000) and Kafdrop (9001 ← 9000) —
  pick a host port out of conflict.
- **RocksDB state backend.** The default `hashmap` backend keeps
  all state in JVM heap, which is fine for tiny state but caps at
  the heap size. The fraud job holds broadcast state
  (~5–10 MB customer features) plus per-key velocity-window
  counters (~100K cards × tens of bytes each); on the heap that's
  fine, but we'd rather not have to retune when the workload grows.
  RocksDB spills to disk and gets us memory-safe defaults.
- **Shared `flink-data` volume.** JM and TM both mount the same
  named volume at `/opt/flink/data`. The JM writes the checkpoint
  metadata; the TM writes the actual operator state files. They
  must agree on the path or restore-from-checkpoint silently
  fails.
- **`HADOOP_CONF_DIR`-equivalent via bind mount.** Flink reads HDFS
  via the Hadoop client config at `/opt/hadoop-conf`, same pattern
  as Spark. The streaming job's bootstrap (broadcast-load
  `/analytics/customer_features`) needs this; without it, Flink
  defaults to `file:///` and the bootstrap silently sees an empty
  feature store.
- **Kafka connector is bundled.** Unlike Spark, Flink ships its
  Kafka connector inside the image. No `--packages` equivalent
  needed at submit time.

## Alternatives

Where Flink sits in the stream-processing landscape:

| System                          | Lineage                          | Pick instead when…                                                                                       |
|---------------------------------|----------------------------------|----------------------------------------------------------------------------------------------------------|
| **Spark Structured Streaming**  | Spark's streaming API            | You want one engine for batch + streaming and can tolerate micro-batch (~seconds) latency + processing-time semantics. We picked Flink because event-time + exactly-once via Kafka two-phase commit matter for fraud scoring. |
| **Kafka Streams**               | JVM library, no separate cluster | You're 100% Kafka-shop and want to avoid running another distributed system. State + scaling are bounded to Kafka itself. Smaller surface, less powerful. |
| **Apache Beam**                 | Abstraction over runners         | You want to write once, run on Flink / Dataflow / Spark. Adds an abstraction tax and lags Flink-native features. |
| **AWS Kinesis Data Analytics**  | Managed Flink                    | You're in AWS and want zero-ops Flink.                                                                   |
| **Materialize / RisingWave**    | Streaming SQL databases          | You want to define streaming jobs as SQL views with incremental view maintenance. Very different model — view-as-job, not job-as-DAG. |
| **Apache Storm**                | Twitter's stream processor       | Practically never. Largely superseded by Flink.                                                          |
| **Apache Samza**                | LinkedIn's Kafka-native processor | Tightly Kafka-coupled, smaller community than Flink today.                                              |

Flink is the **default 2026 pick** when you genuinely need event-
time semantics + exactly-once + stateful operators at scale.
Spark Structured Streaming is the cheaper-ops alternative if you
already have Spark and your use case is forgiving on those three
properties.

## Common commands

Submit a JAR job:

```sh
docker compose exec flink-jobmanager flink run \
    -d /opt/flink/usrlib/<artifact>.jar
```

Submit a PyFlink job:

```sh
docker compose exec flink-jobmanager flink run \
    -d -py /opt/flink/usrlib/<job>.py
```

List + cancel:

```sh
docker compose exec flink-jobmanager flink list
docker compose exec flink-jobmanager flink cancel <jobid>
```

Manual savepoint (handy before a code upgrade):

```sh
docker compose exec flink-jobmanager flink savepoint <jobid>
docker compose exec flink-jobmanager flink run -s <savepoint-path> ...
```

UIs:

- Flink jobmanager: <http://localhost:8082>

```sh
make smoke-flink   # /overview reachable + ≥ 1 taskmanager registered
```

## Caveats

- **Checkpoints survive restart, not `make nuke`.** A scheduled
  scheduler restart is fine; a `make nuke` resets the volume.
  Save savepoints somewhere durable (e.g. HDFS) before any
  destructive cleanup if you care about exactly-once continuity
  across rebuilds.
- **`/opt/flink/usrlib` is read-only.** Job artifacts come from
  the `./src/consumer` bind mount; edit on the host, re-submit
  with `flink run`. Don't try to write into this directory from
  inside the container.
- **Watermarks lag the slowest source.** If you ever add a second
  source (e.g. broadcast-load device fingerprints alongside
  customer features), watermark advancement halts on whichever
  source is idle. Configure `idleness` on the source operator to
  avoid stuck windows.
- **Web UI restart-button vs `flink cancel`.** The UI's *Cancel
  Job* button cancels without taking a savepoint; for upgrades
  use `flink cancel -s <dir>` so you can resume.
- **`taskmanager.host` mismatch is silent.** If the TM advertises
  a hostname the JM can't reach, registration just retries
  forever and `flink list` shows no slots available. The smoke
  probe catches this; if a real job hangs at *Created*, the slot
  count in the JM UI is the first thing to look at.
