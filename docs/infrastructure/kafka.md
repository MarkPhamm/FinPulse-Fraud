# Kafka

**Source of truth for the `transactions` fact stream.** Per the
dataflow plan, transactions never land in HDFS — the producer
publishes `transactions.csv.gz` directly to Kafka topic
`transactions` and that topic *is* the system's record of truth.
Two consumers read it: Spark in batch (nightly, by offset range)
and Flink in streaming (continuously). Flink writes scored output
back to two more topics (`transactions-scored`, `fraud-alerts`).

[Kafdrop](https://github.com/obsidiandynamics/kafdrop) is a
lightweight web UI mounted on top of the same broker so you can
poke at topics, partitions, and consumer groups without dropping
into a shell.

## Topology

| Service   | Image                                  | Hostname  | Host → container ports | What other services talk to it                              |
|-----------|----------------------------------------|-----------|------------------------|-------------------------------------------------------------|
| `kafka`   | `apache/kafka:3.7.1`                   | `kafka`   | `9092→9092`            | Host clients on :9092; in-network clients on `kafka:9094`   |
| `kafdrop` | `obsidiandynamics/kafdrop:latest`      | `kafdrop` | `9001→9000`            | Browser → Kafdrop UI; Kafdrop → `kafka:9094`                |

Single-broker KRaft cluster — same node is both broker and
controller. No external ZooKeeper for Kafka itself (Pinot has its
own — see [`pinot.md`](pinot.md)).

### The three listeners

| Listener     | Port | Used by                                                        |
|--------------|------|----------------------------------------------------------------|
| `EXTERNAL`   | 9092 | Host-machine clients — advertised as `localhost:9092`          |
| `INTERNAL`   | 9094 | Other docker containers — advertised as `kafka:9094`           |
| `CONTROLLER` | 9093 | KRaft controller quorum (intra-cluster only)                   |

A producer running on your laptop connects to `localhost:9092`; a
Spark worker container connects to `kafka:9094`. Cross the wires
and you'll see *connection refused* (using `kafka:9094` from the
host) or *advertised listener loop* errors (using `localhost:9092`
from inside the docker network).

`KAFKA_INTER_BROKER_LISTENER_NAME=INTERNAL` says replicas
intra-cluster talk over the INTERNAL listener. With one broker
that's effectively a no-op, but if you ever scale out, this is the
right default.

## Configuration source

Pure environment variables on the `kafka` service. The official
`apache/kafka` image uses the form `KAFKA_<KEY>` (no `_CFG_` infix
that Bitnami's image uses — easy to mix up if you copy snippets
from the wrong tutorial).

The non-trivial ones, all set in
[`docker-compose.yml`](../../docker-compose.yml):

| Env var                                          | Value                                | Why                                                              |
|--------------------------------------------------|--------------------------------------|------------------------------------------------------------------|
| `KAFKA_NODE_ID`                                  | `0`                                  | Single-broker, single-controller                                 |
| `KAFKA_PROCESS_ROLES`                            | `controller,broker`                  | Combined-mode KRaft                                              |
| `KAFKA_LISTENERS`                                | `INTERNAL://:9094,CONTROLLER://:9093,EXTERNAL://:9092` | Bind addresses                                       |
| `KAFKA_ADVERTISED_LISTENERS`                     | `INTERNAL://kafka:9094,EXTERNAL://localhost:9092`    | What the broker tells clients to connect to          |
| `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP`           | `*:PLAINTEXT`                         | Class project — no TLS                                           |
| `KAFKA_CONTROLLER_QUORUM_VOTERS`                 | `0@kafka:9093`                        | Single-voter quorum                                              |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE`                | `true`                                | Producers can create `transactions`/`transactions-scored`/`fraud-alerts` on first publish |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`         | `1`                                   | **Required for single broker** — default is 3, refuses to create |
| `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR` | `1`                                   | Same as above for the transactions/exactly-once topic            |
| `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR`            | `1`                                   | Same as above                                                    |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS`         | `0`                                   | Tests / smoke runs don't pay the 3-second startup tax            |
| `KAFKA_LOG_DIRS`                                 | `/var/lib/kafka/data`                 | Pointed at the named volume                                      |

Kafdrop is even simpler — one var: `KAFKA_BROKERCONNECT=kafka:9094`.

## Volumes

| Volume       | Mount path             | What's persisted              | Wiped by `make nuke` |
|--------------|------------------------|-------------------------------|----------------------|
| `kafka-data` | `/var/lib/kafka/data`  | Topics, partitions, KRaft metadata, offsets | yes      |

The first boot auto-formats the log directory and generates a
cluster ID; subsequent boots reuse it. Kafdrop has no volume — it's
purely a UI on top of the broker.

## Healthcheck

Kafka:

```yaml
test: /opt/kafka/bin/kafka-broker-api-versions.sh \
        --bootstrap-server kafka:9094 > /dev/null
interval: 10s
timeout:  5s
retries:  12
```

`kafka-broker-api-versions.sh` is the cheapest probe that actually
exercises a live connection. Other services that depend on Kafka
(`kafdrop`, `flink-jobmanager`) wait for `service_healthy` before
starting. Kafdrop has no healthcheck.

## Why this shape

- **Single broker.** Class project scale. The replication overrides
  above are the only quirks; everything else is stock.
- **KRaft, not ZooKeeper.** Apache 3.7 supports KRaft as production-
  ready and the future is ZK-less. Eliminates one container and
  one healthcheck race.
- **Three listeners on purpose.** Cleanly separating host clients
  from in-network clients from the controller quorum keeps the
  *"why isn't my consumer connecting?"* question always answerable
  by looking at which network the client lives on.
- **`KAFKA_AUTO_CREATE_TOPICS_ENABLE=true`.** Cuts the producer
  bring-up to one step (just publish — the broker creates the
  topic). For production you'd disable this and pre-create with
  explicit partition counts and retention. The class project's
  three topics use defaults today; revisit when we tune
  `transactions` retention to support a long Spark batch replay.
- **Kafdrop on `:latest`, not a pinned tag.** `4.0.1` is amd64-only;
  `:latest` is multi-arch (verified Apr 2026). Pin to a digest if
  reproducibility ever bites.
- **Kafdrop port-remap (host 9001 ← container 9000).** Kafdrop's
  default UI port is 9000, which collides with the HDFS NameNode
  RPC port. Same trick as Pinot Controller (9100 ← 9000).
- **`user: "0:0"` on Kafka.** Lets the entrypoint chown
  `/var/lib/kafka/data` on first boot when Docker creates the
  volume with root ownership.

## Alternatives

Where Kafka sits in the messaging / streaming landscape:

| System                  | Lineage                          | Pick instead when…                                                                                          |
|-------------------------|----------------------------------|-------------------------------------------------------------------------------------------------------------|
| **RabbitMQ**            | AMQP message broker              | Traditional work queues + per-message ack semantics. RabbitMQ deletes messages after consumption — there's no replay-from-offset, which is *the* feature we need here. |
| **AWS Kinesis**         | Managed Kafka-equivalent         | You're in AWS and want zero ops. Lower throughput per shard than per Kafka partition, replay window capped at 7 days, vendor lock-in. |
| **Google Pub/Sub**      | Managed cloud pub/sub            | You're in GCP. Auto-scales (no shard concept), but replay is by snapshot, not by offset — different mental model. |
| **Azure Event Hubs**    | Managed pub/sub on Azure         | You're in Azure. Kafka-protocol-compatible mode means most clients drop in unchanged.                       |
| **Apache Pulsar**       | Kafka competitor                 | You need multi-tenancy + tiered storage built in. More moving parts (BookKeeper + ZooKeeper).               |
| **NATS / NATS JetStream** | Lightweight messaging          | You want sub-ms latency at smaller scale, with simpler ops than Kafka.                                      |
| **Redis Streams**       | In-memory streams                | Tiny streams (< 1M msgs/day) where Kafka is overkill.                                                       |

Kafka won the **durable replayable log** category and that's
exactly what we need here — Spark batch-reads the same topic Flink
streams from. RabbitMQ would have us writing transactions to two
places to support both consumers. Pulsar is the strongest 2026
challenger but has more operational surface.

## Common commands

Use the in-network listener `kafka:9094` from inside containers:

```sh
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 --list

docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 \
    --create --topic transactions --partitions 4 --replication-factor 1

docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server kafka:9094 --topic transactions \
    --from-beginning --max-messages 5
```

From the host (uses the EXTERNAL listener):

```sh
kafkactl --brokers localhost:9092 get topics    # if you have kafkactl
```

UIs:

- Kafdrop: <http://localhost:9001>

```sh
make smoke-kafka   # ephemeral topic + produce + consume + delete
```

## Caveats

- **Replication is hard-coded to 1.** The internal-topic RF
  overrides are mandatory for a single broker. If you ever add a
  second broker, *also* bump those to 3 (and add `--replication-
  factor 3` on every topic create), otherwise replicas won't be
  placed on the new broker.
- **`auto-create` has invisible defaults.** Auto-created topics get
  `num.partitions=1` and `default.replication.factor=1`. The
  `transactions` topic gets used heavily by both Spark batch and
  Flink streaming; if you want partition-level parallelism (Flink
  scoring with parallelism > 1, partitioning by `card_id`),
  pre-create the topic with the right partition count *before* the
  producer first publishes.
- **`make nuke` deletes all topics + offsets.** No surprise — the
  named volume is the only state — but worth knowing if you've
  built up consumer-group offsets you care about.
