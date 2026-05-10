# Step 3 — Kafka producer: replay transactions as a stream

This step creates the Kafka topic `transactions` and a host-side Python
script that streams `data/transactions.csv.gz` into it, keyed by
`card_id`. After this step, **Kafka is the single source of truth for
the transaction fact** — Step 4 (Spark batch) and Step 7 (Flink
streaming) both consume from this same topic.

This is the sister doc to
[`docs/plans/plan.md` § Step 3](../plans/plan.md#step-3--kafka-producer-replay-transactions-as-a-stream)
— the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact                                  | Purpose                                                                |
|-------------------------------------------|------------------------------------------------------------------------|
| `producer` compose service                | Lightweight Python container (`python:3.11-slim` + `kafka-python`)     |
| Kafka topic `transactions`                | Long-retention (`retention.ms=-1`) topic with 6 partitions             |
| `src/producer/replay_transactions.py`     | Script (run inside the `producer` container) that publishes CSV rows  |

`transactions.csv.gz` has these columns (preview at
[`data/preview/transactions.csv`](../../data/preview/transactions.csv)):

```text
txn_id, timestamp, card_id, merchant_id, amount, currency,
merchant_category, country, channel, is_international
```

The total is ~1,000,000 rows over a ~6-month window. We will publish
each row as a JSON value, with `card_id` as the Kafka **message key**.

## What this step does NOT do

- **No consumer.** The Spark batch consumer is Step 4; Flink streaming
  is Step 7. This step only fills the topic.
- **No transformation.** The JSON body mirrors the CSV row 1:1 — no
  schema cleanup, no enrichment, no joins. The "Kafka is the
  source of truth" guarantee depends on what we publish staying
  faithful to the source file.
- **No HDFS write.** Per
  [`docs/plans/dataflow.md`](../plans/dataflow.md), `transactions` is a
  Kafka-only stream. There must be no `/landing/transactions/` and no
  `/curated/transactions/` after this step.
- **No exactly-once semantics yet.** We turn on producer idempotence
  (deduplicates within a session), but full exactly-once across
  consumer reprocessing is a Step 7 concern (Flink + transactional
  Kafka sink).

## Concepts you'll meet here

- **Kafka topic = append-only partitioned log.** A topic is split into
  N **partitions**; each partition is a strictly ordered log on a
  single broker. With one broker, all partitions live here, but the
  partitioning still matters: **records with the same key always go
  to the same partition**, so per-key ordering is preserved.
- **Why key by `card_id`.** Velocity-attack detection in Step 7 looks
  at *the last 10 minutes of activity for one card*. If the same card
  could land on two partitions, two parallel Flink tasks would each
  see half the events and miss the burst. Keying by `card_id` makes
  "all of this card's history is in order on one partition" a Kafka
  guarantee, not a hope.
- **Internal vs external listener.** From inside the docker network
  (Spark / Flink / Kafdrop / our new `producer` container), you connect
  on `kafka:9094`. From your host shell, you'd use `localhost:9092`.
  Because the producer runs in a container, it uses `kafka:9094` —
  same as everything else in the stack. The `9092` listener is there
  for human debugging from your laptop, not for the application.
  Mixing these up is the most common Kafka beginner trap (see
  CLAUDE.md § "Kafka has three listeners").
- **`retention.ms=-1`.** Kafka's default retention is 7 days — old
  segments get deleted to reclaim disk. For a system-of-truth topic
  (the Robinhood pattern: same Kafka topic feeds both real-time Pinot
  ingest and the nightly Spark reconciliation), you want the topic to
  **never expire**. `-1` disables time-based deletion. Without it,
  Step 4's "batch-read the entire topic" silently truncates the
  historical record.
- **At-least-once with `acks="all"`.** The producer waits for the
  broker's leader to confirm the write before treating a `send()` as
  successful. If a network blip causes a retry, Kafka may write the
  message twice — so we get **at-least-once**, not exactly-once.
  Conceptually, "exactly-once" comes from two more pieces: (1) an
  *idempotent producer* (sequence number per partition so the broker
  dedupes retries) and (2) *transactional producer* (atomic commit
  across multiple partitions/topics). The Java client and
  `confluent-kafka` Python both support these; **`kafka-python`
  (the pure-Python lib we use here) supports neither.** That's a
  deliberate trade-off — kafka-python is one `pip install` away,
  no native deps. Real exactly-once for our pipeline lands in
  **Step 7** (Flink + a transactional Kafka sink, two-phase commit),
  which is the right layer for it anyway.
- **Throttling vs realism.** The 1M txns span 6 months of wall time.
  Three replay modes:
  1. **Throttled flat rate** (`--rate 200` → 200 txn/sec) — finishes
     in ~80 minutes, simple to reason about. **This is what we ship.**
  2. **Real-time replay** — emit at the timestamp's wall-clock offset
     scaled by `--speedup N`. Closer to production; harder to debug.
  3. **Burst mode** — pump as fast as possible. Stress-test, doesn't
     exercise event-time windowing.
  We'll build (1). (2) and (3) are easy follow-ups if you want them.

## Pre-flight

```sh
# 1. Kafka is up and healthy (the broker — not just the container).
docker compose ps kafka kafdrop
# Both should be Up; kafka should report (healthy).

# 2. The smoke topic round-trip still passes (sanity that Kafka is
#    actually serving requests, not just running).
make smoke-kafka
# expect: green "OK: Kafka produce/consume works"

# 3. The 'transactions' topic does NOT exist yet — we create it in 3b.
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 --list
# expect: empty (or only Kafka's __ internal topics — none start with 'transactions').

# 4. The 'producer' service is NOT yet defined — we add it in 3a.
#    (Use `compose config --services` rather than `compose ps producer`:
#    ps prints "no such service: producer" to stderr, which a naive
#    grep would mistake for the service existing.)
docker compose config --services | grep -qx producer && echo "EXISTS" || echo "OK: not yet"

# 5. Kafdrop UI is reachable on http://localhost:9001
curl -fsS http://localhost:9001 -o /dev/null && echo OK
```

If any of those fail, fix that first. Every sub-step assumes this baseline.

### Reading check #3 — `kafka-topics.sh --list`

This one's worth a closer look — it's a different *family* of Kafka
tool from the consumer/producer pair we'll use in the warmup.

- **`/opt/kafka/bin/kafka-topics.sh`** — Kafka's **topic
  administration** CLI. Different from `kafka-console-{consumer,
  producer}.sh`: those move *records* (data plane), this one queries
  and mutates *cluster metadata* (control plane). Same network,
  completely different responsibility — the mental separation matters
  for debugging.
- **`--bootstrap-server kafka:9094`** — same idea as on the consumer.
  We dial any broker as an entry point; the cluster routes the admin
  request to whichever broker is currently the **controller** (the
  one that owns metadata mutations). Single-broker means it's the
  only one, so the indirection is invisible — but the flag exists
  for the same reason on every CLI.
- **`--list`** — the subcommand. `kafka-topics.sh` is built like
  `git`: one binary, many subcommand-style flags. The ones you'll see
  in this project:
  - `--list` — print one topic name per line (what you ran).
  - `--describe --topic <name>` — show partition count, replication
    factor, leader/ISR per partition, plus any non-default configs
    (`retention.ms`, `segment.bytes`, …). 3b's verify step uses this.
  - `--create --topic <name> --partitions N --replication-factor R
    [--config k=v]` — what 3b uses to create `transactions`.
  - `--delete --topic <name>` — opposite of create.
  - `--alter --topic <name> --partitions N` — grow partition count.
    Kafka lets you grow but never shrink (rebalancing the keyspace
    backwards is hard).

The output `__consumer_offsets` you'll see is a Kafka **internal
topic** — Kafka stores consumer-group state (where each group last
read in each partition) as records in this topic; it eats its own
dog food. Anything starting with `__` (double underscore) is
internal by convention. Once Step 7 runs transactional Flink writes,
you'll also see `__transaction_state` show up.

If you ever want only the user topics, the idiom is
`kafka-topics.sh --list | grep -v '^__'`.

## Warmup — Hand-roll one produce/consume round-trip

Before touching Python, send and receive one message using Kafka's
shell tools. This is the same idea as Step 1's "manual `hdfs dfs -put`
on one file before writing the loader" — see each piece work on its
own so when the script fails you know which layer to blame.

In **terminal A**, start a console consumer that watches a temporary
topic:

```sh
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server kafka:9094 \
    --topic warmup \
    --from-beginning \
    --property print.key=true \
    --property key.separator=" | "
# Sit and wait — this blocks until messages arrive.
```

### Reading the consumer command

Each piece, in order:

- **`docker compose exec kafka`** — run something inside the already-up
  `kafka` container, where Kafka's bundled CLI tools live (under
  `/opt/kafka/bin/`). Same shape as `docker compose exec namenode hdfs
  dfs ...` from Step 1; `<service>` is just the docker-compose service
  name.
- **`/opt/kafka/bin/kafka-console-consumer.sh`** — Kafka's built-in
  topic-tail tool. Conceptually it's `tail -f` for a Kafka topic:
  subscribe → print → block → repeat. Ships in every `apache/kafka`
  image; no install needed.
- **`--bootstrap-server kafka:9094`** — the broker to dial. We're
  inside the docker network, so we use the **internal** listener
  `kafka:9094`, not the host listener `localhost:9092`. The word
  *bootstrap* is literal: this is just an entry point — once
  connected, the broker tells the client about the rest of the
  cluster (in our case, only itself, because single-broker).
- **`--topic warmup`** — which topic to read from. We haven't created
  `warmup` explicitly; the broker has `KAFKA_AUTO_CREATE_TOPICS_ENABLE=true`
  set in `docker-compose.yml`, so it gets created on first reference
  with the broker defaults (`num.partitions=1`, 7-day retention). For
  a throwaway warmup topic that's fine; for `transactions` in 3b we
  create it explicitly because we want non-default settings.
- **`--from-beginning`** — start reading at **offset 0** of every
  partition, not at the live tail. This is the single most-important
  flag in the command. Without it, the consumer subscribes only to
  *future* messages, so the three records you push from terminal B
  would still arrive — but if the producer had run *before* the
  consumer started, those records would be invisible. New consumers
  default to `latest` (live tail) precisely because production
  consumers don't want to re-read history every restart.
- **`--property print.key=true`** — also print each record's **key**.
  By default the consumer prints only the value, which would hide the
  whole point of this exercise (showing keyed-partitioning).
- **`--property key.separator=" | "`** — what string to print between
  key and value. Default is a tab; `" | "` is just easier to eyeball
  in the terminal. (This flag is purely cosmetic; the producer's
  `key.separator` later is *not* — see below.)

In **terminal B**, push a few keyed messages:

```sh
docker compose exec -T kafka /opt/kafka/bin/kafka-console-producer.sh \
    --bootstrap-server kafka:9094 \
    --topic warmup \
    --property parse.key=true \
    --property key.separator=":" <<'EOF'
CARD-001:{"txn_id":"TXN-1","amount":12.50}
CARD-002:{"txn_id":"TXN-2","amount":99.00}
CARD-001:{"txn_id":"TXN-3","amount":7.25}
EOF
```

### Reading the producer command

Differences from the consumer command:

- **`-T` on `compose exec`** — disables TTY allocation. We need this
  because we're piping a heredoc into the command's stdin. With a TTY
  attached, Docker would error out with *"the input device is not a
  TTY"*. Rule of thumb: any `compose exec` that takes piped input
  needs `-T`.
- **`kafka-console-producer.sh`** — the producer counterpart to the
  consumer. Reads stdin **one line at a time** and sends each line as
  one record.
- **`--property parse.key=true` + `--property key.separator=":"`** —
  these two together tell the producer "split each input line on the
  first `:`; left half is the key, right half is the value." So
  `CARD-001:{"txn_id":"TXN-1",...}` becomes a record with key
  `CARD-001` and value `{"txn_id":"TXN-1",...}`. Without
  `parse.key=true`, the whole line would be the value and the key
  would be `null` — defeating the keyed-partitioning demonstration.
  Note this `key.separator` is **semantic** (controls how input is
  parsed), not cosmetic like the consumer's same-named flag.
- **`<<'EOF' ... EOF`** — bash heredoc. Everything between the two
  `EOF` markers is fed into the command's stdin as if you'd typed it.
  The single quotes around `'EOF'` disable shell expansion inside the
  block, so the JSON braces and quotes pass through untouched.

Terminal A should immediately print:

```text
CARD-001 | {"txn_id":"TXN-1","amount":12.50}
CARD-002 | {"txn_id":"TXN-2","amount":99.00}
CARD-001 | {"txn_id":"TXN-3","amount":7.25}
```

Open Kafdrop at <http://localhost:9001>, click the `warmup` topic,
and notice that both `CARD-001` messages are on the **same partition**
even though the topic has multiple. That's the keyed-partitioning
guarantee we'll rely on for `card_id` in the real producer.

Stop terminal A with `Ctrl+C` and clean up:

```sh
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 --delete --topic warmup
```

The remaining sub-steps turn this round-trip into a Python script that
reads the CSV, JSON-encodes each row, keys by `card_id`, and pushes
all 1M rows into a properly-configured `transactions` topic.

## 3a — Add a `producer` compose service

Everything else in this stack runs in a container — Spark, Flink,
Kafka, even one-shot deps via `make hive-deps`. The producer should
follow the same posture: keep your laptop dep-free, drive the work
from inside the docker network, talk to `kafka:9094` like every
other service does.

Add the following service to [`docker-compose.yml`](../../docker-compose.yml),
right after the `kafdrop` block (it belongs in the Kafka section):

```yaml
  # Step 3 producer — replays data/transactions.csv.gz into Kafka topic
  # 'transactions'. Containerised so the host stays Python-dep-free;
  # same posture as the rest of the stack. kafka-python installs at
  # container startup (mirrors the Superset requirements-local.txt
  # pattern — fetch one dep at boot rather than baking an image for it).
  # Idle on `sleep infinity` so you can drive it via `compose exec`.
  producer:
    image: python:3.11-slim
    container_name: producer
    command:
      - sh
      - -c
      - "pip install --no-cache-dir kafka-python==2.0.2 && exec sleep infinity"
    volumes:
      - ./src/producer:/opt/producer:ro
      - ./data:/data:ro
    depends_on:
      kafka:
        condition: service_healthy
    networks: [finpulse]
    restart: unless-stopped
```

Things worth pointing out before you paste this in:

- **`sleep infinity`.** The container has no real "main process" — its
  job is to host a Python interpreter we drive ad-hoc. Without
  `sleep infinity`, the container would exit as soon as `pip install`
  finished and `compose exec` would have nowhere to land.
- **`./data:/data:ro`.** Read-only bind so the script can read
  `/data/transactions.csv.gz` directly. `ro` is defence-in-depth —
  we never write to source data.
- **`./src/producer:/opt/producer:ro`.** Same shape as `jobs/` for
  Spark: edit on your host, the change shows up inside the container
  immediately, no rebuild.
- **`pip install` at startup, not in a Dockerfile.** Same posture as
  Superset's `docker/superset/requirements-local.txt` — one dep
  doesn't earn an image build. Adds ~5–10 s to the first start;
  cached thereafter for the lifetime of the container.
- **`depends_on: kafka (healthy)`.** Without this, `up -d producer`
  could race `up -d kafka` and fail the first send. The health
  predicate makes the ordering explicit.

Bring it up and confirm `kafka-python` actually installed:

```sh
docker compose up -d producer
docker compose logs producer --tail=20
# expect: "Successfully installed kafka-python-2.0.2"

docker compose exec producer python -c "import kafka; print(kafka.__version__)"
# expect: 2.0.2
```

Verify the bind-mounts landed:

```sh
docker compose exec producer ls /opt/producer  /data
# /opt/producer  → empty (we add replay_transactions.py in 3c)
# /data          → the five .gz files + preview/
```

Commit as `step 3a: add producer compose service`.

## 3b — Create the `transactions` topic (one-time)

The topic must exist before the producer can write to it, and it must
have the right config from the start. Auto-created topics get the
broker defaults (`num.partitions=1`, `retention.ms=604800000` = 7
days), neither of which is what we want for a system-of-truth fact
stream.

What we want:

| Setting               | Value         | Why                                                                                  |
|-----------------------|---------------|--------------------------------------------------------------------------------------|
| `--partitions`        | `6`           | Card-id keyed parallelism: 6 lanes is plenty for 1M rows / our toy Flink slot count. |
| `--replication-factor`| `1`           | Single-broker cluster — RF can't exceed broker count.                                |
| `retention.ms`        | `-1`          | Topic is the long-term record; no time-based deletion.                                |
| `segment.bytes`       | `104857600`   | 100 MB segment files; small enough to be readable, big enough to avoid file-spam.    |

Create it:

```sh
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 \
    --create --if-not-exists --topic transactions \
    --partitions 6 --replication-factor 1 \
    --config retention.ms=-1 \
    --config segment.bytes=104857600
```

Verify the config landed:

```sh
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 \
    --describe --topic transactions
# expect: PartitionCount: 6, ReplicationFactor: 1,
#         Configs: retention.ms=-1,segment.bytes=104857600
```

Open Kafdrop → `transactions` topic → confirm 6 partitions, all empty
(message count = 0).

`--if-not-exists` makes this command idempotent — running it again is
a no-op. We don't ship a Python wrapper for it; one-shot infra setup
is fine as a hand-run command, like Step 0's `make hive-deps`.

Commit as `step 3b: create transactions Kafka topic`.

## 3c — Minimal producer: read CSV → JSON → Kafka

Time for Python. Create `src/producer/replay_transactions.py` on
your host — the file is bind-mounted into the producer container at
`/opt/producer/replay_transactions.py`, so edits show up there
immediately.

First iteration: read the gz CSV, JSON-encode each row, push to
`transactions` keyed by `card_id`. No throttling, no CLI args — just
prove the pipeline works end-to-end on a tiny `--limit`.

```python
"""Replay data/transactions.csv.gz into Kafka topic 'transactions'.

Each CSV row becomes one Kafka message:
  - key   = card_id (bytes) — same card → same partition, ordering preserved
  - value = JSON-encoded row (bytes)

Run from inside the producer container:
    docker compose exec producer python /opt/producer/replay_transactions.py
"""
from __future__ import annotations

import csv
import gzip
import json

from kafka import KafkaProducer

CSV_PATH = "/data/transactions.csv.gz"   # bind-mounted from ./data
TOPIC = "transactions"
BOOTSTRAP = "kafka:9094"                  # internal listener (we're inside the network)


def main() -> None:
    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: k.encode("utf-8"),
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks="all",
    )

    sent = 0
    with gzip.open(CSV_PATH, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            producer.send(TOPIC, key=row["card_id"], value=row)
            sent += 1
            if sent % 1000 == 0:
                print(f"  sent {sent} rows")

    producer.flush()
    producer.close()
    print(f"\nDone. Sent {sent} rows to topic '{TOPIC}'.")


if __name__ == "__main__":
    main()
```

A few things worth pointing out:

- **`acks="all"`.** The producer waits for the broker to confirm the
  write before treating it as sent. With one broker that's just the
  leader, but the contract is the same one you'd use in a real cluster.
  Combined with `kafka-python`'s default of unlimited retries, this
  gives us **at-least-once** delivery (a network blip mid-flight may
  cause one duplicate). True exactly-once needs an idempotent and/or
  transactional producer, neither of which `kafka-python` supports —
  see Concepts above. We accept at-least-once here; Step 7 (Flink)
  is where exactly-once actually lands.
- **`flush()` before `close()`.** `send()` is async — it batches into
  an in-memory buffer. `flush()` blocks until everything in the buffer
  has been ack'd. Without it, you may exit before the last batch is
  on the wire.
- **No CLI args yet.** That's 3d.

Don't run this against the full 1M rows yet. Add a temporary
`if sent >= 100: break` and run it once, then watch the topic:

```sh
# Terminal A: tail the topic.
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server kafka:9094 \
    --topic transactions --from-beginning --max-messages 5 \
    --property print.key=true --property key.separator=" | "

# Terminal B: drive the producer container.
docker compose exec producer python /opt/producer/replay_transactions.py
```

### Reading the run commands

**Terminal A** is the same `kafka-console-consumer.sh` invocation as
the warmup, with two differences:

- **`--topic transactions`** instead of `warmup` — we're tailing the
  real topic now (the one we created in 3b with 6 partitions and
  long retention).
- **`--max-messages 5`** (new) — tell the consumer to exit cleanly
  after 5 records, rather than blocking forever like the warmup did.
  Handy for one-shot verifications: print a sample, then quit back
  to your shell. Without it you'd be on `Ctrl+C` duty.

Everything else (`--bootstrap-server`, `--from-beginning`,
`--property print.key=true`, `--property key.separator`) is identical
to the warmup — same explanations apply.

**Terminal B** runs the Python script you just wrote, inside the
`producer` container from 3a:

- **`docker compose exec producer`** — same `compose exec` shape
  we've been using, but pointed at the `producer` service. That
  container is sitting on `sleep infinity` waiting for work; this
  is the work.
- **`python /opt/producer/replay_transactions.py`** — invoke the
  script at its in-container path. `/opt/producer/` is the bind-mount
  from `./src/producer/` (set in the compose volume list in 3a), so
  the file you just edited on your host is exactly what runs — no
  copy-in, no rebuild.
- `kafka-python` was `pip install`-ed at container start (3a), so
  the `from kafka import KafkaProducer` line resolves.
- No `--rate` / `--limit` yet — those are 3d.

Expected: the consumer prints 5 lines like
`CARD-043960 | {"txn_id":"TXN-0000001","timestamp":"2025-06-15 09:59:52",...}`
and exits. Kafdrop's `transactions` topic now reports 100 messages,
spread across the 6 partitions roughly proportionally to the
`card_id` distribution (it won't be exactly 100/6 = 16.7 per partition
because hashing is not uniform on small samples).

Remove the temporary `break` once you've seen this work.

Commit as `step 3c: minimal Kafka producer for transactions`.

## 3d — Add `--rate` and `--limit`

Two CLI flags, one for testing, one for not melting your laptop:

- `--rate N` — cap throughput at N messages/second. Default `200` is
  ~80 minutes for the full 1M.
- `--limit N` — stop after N messages. Useful for re-runs without
  re-publishing all 1M.

Throttling pattern: track the wall-clock budget as
`start + sent / rate`, and `time.sleep(...)` whenever you're ahead of
schedule. This is `--rate` mode (1) from the Concepts list. Modes (2)
and (3) are easy follow-ups; ship (1) first.

Add to the script:

```python
import argparse
import time


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rate", type=int, default=200,
                   help="max messages per second (default: 200)")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after N messages (default: send all rows)")
    return p.parse_args()
```

Update `main()` to take args, throttle, and stop early:

```python
def main() -> None:
    args = parse_args()
    producer = KafkaProducer(...)   # unchanged

    sent = 0
    start = time.monotonic()
    with gzip.open(CSV_PATH, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if args.limit is not None and sent >= args.limit:
                break

            producer.send(TOPIC, key=row["card_id"], value=row)
            sent += 1

            # Pace: sleep until 'sent' is back on the rate budget.
            target = start + sent / args.rate
            now = time.monotonic()
            if now < target:
                time.sleep(target - now)

            if sent % 1000 == 0:
                elapsed = time.monotonic() - start
                print(f"  sent {sent} rows in {elapsed:.1f}s "
                      f"({sent / elapsed:.0f} msg/s)")

    producer.flush()
    producer.close()
    elapsed = time.monotonic() - start
    print(f"\nDone. Sent {sent} rows to topic '{TOPIC}' "
          f"in {elapsed:.1f}s ({sent / elapsed:.0f} msg/s).")
```

Test the new flags on a small batch first:

```sh
docker compose exec producer python /opt/producer/replay_transactions.py \
    --rate 100 --limit 1000
# Should take ~10 seconds and print msg/s ≈ 100.
```

Open Kafdrop — the `transactions` topic should now show roughly
1,100 messages (100 from 3c + 1,000 from this run), still keyed by
card, still spread across 6 partitions.

Commit as `step 3d: add --rate and --limit flags to replay_transactions`.

## 3e — Full 1M replay

With small-batch verification working, kick off the full replay. At
`--rate 200` this takes ~80 minutes; bump to `--rate 1000` if you're
in a hurry and your laptop can keep up (kafka-python on a single
producer thread tops out around 5–10 K msg/s on a modern machine, so
1000 is well within reach).

```sh
docker compose exec producer python /opt/producer/replay_transactions.py \
    --rate 1000
```

Watch progress in either:

- **The container's stdout** — every 1000 rows the script prints
  elapsed time and effective msg/s. Tail with
  `docker compose logs producer --follow` if you want it in another
  window.
- **Kafdrop** at <http://localhost:9001> — refresh the topic page;
  the message count climbs in near-real-time.

If the producer is interrupted mid-run, just re-run it. The topic
already has whatever it sent, so you'll end up with **more than 1M
messages** if you start from scratch — that's fine for class scope,
because Step 4 batch-reads by offset range, not by row count, and the
consumer is content with duplicate `txn_id`s as long as the joins
deduplicate downstream. (If you want a clean 1M, delete the topic
first: `kafka-topics.sh --delete --topic transactions` then redo 3b.)

Commit as `step 3e: full transactions replay into Kafka`.

## Verification

Run all five checks. The first three are the rubric; the last two
are sanity.

```sh
# 1. Topic exists with the right config.
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server kafka:9094 --describe --topic transactions
# expect: PartitionCount: 6, retention.ms=-1, segment.bytes=104857600

# 2. Topic has ~1M messages, distributed across all 6 partitions.
docker compose exec kafka /opt/kafka/bin/kafka-run-class.sh \
    kafka.tools.GetOffsetShell \
    --bootstrap-server kafka:9094 --topic transactions
# expect: 6 lines, one per partition, each with offset > 0,
#         summing to ~1,000,000 (or more, if you re-ran after 3b).

# 3. Sample message looks right (keyed, JSON-valued).
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server kafka:9094 --topic transactions \
    --from-beginning --max-messages 3 \
    --property print.key=true --property key.separator=" | "
# expect: 3 lines, each "CARD-XXXXXX | {\"txn_id\":...}"

# 4. Same card → same partition (the ordering guarantee).
#    Pick any card_id from the sample above and confirm all its
#    messages are on one partition. Easiest in Kafdrop:
#       Topics → transactions → "Search messages" → key = CARD-043960
#    All matches should report the same partition number.

# 5. Confirm transactions are NOT in HDFS.
docker compose exec namenode hdfs dfs -ls /landing 2>/dev/null \
    | grep -i transactions || echo "OK: no /landing/transactions/"
```

### What "passes" looks like

```text
$ kafka-topics.sh --describe --topic transactions
Topic: transactions  PartitionCount: 6  ReplicationFactor: 1
  Configs: retention.ms=-1,segment.bytes=104857600
  Topic: transactions  Partition: 0  Leader: 0  ...
  ... (6 partition lines)

$ GetOffsetShell --topic transactions
transactions:0:166842
transactions:1:166703
transactions:2:166791
transactions:3:166624
transactions:4:166938
transactions:5:166102
# sum ≈ 1,000,000; each partition within ~5% of the mean (good hash spread)

$ kafka-console-consumer.sh --max-messages 1 --print.key=true ...
CARD-043960 | {"txn_id":"TXN-0000001","timestamp":"2025-06-15 09:59:52",
              "card_id":"CARD-043960","merchant_id":"MERCH-09873",
              "amount":"179.84","currency":"USD",...}
```

## Troubleshooting

- **`NoBrokersAvailable: kafka:9094`.** The producer container can't
  reach the Kafka broker. Check (a) `docker compose ps producer kafka`
  — both must be Up, kafka should be `(healthy)`; (b) you're using
  `kafka:9094`, not `localhost:9092`, in the script (the host listener
  is unreachable from inside the docker network).
- **`ModuleNotFoundError: No module named 'kafka'`.** The startup
  `pip install` either hasn't finished or failed. `docker compose logs
  producer --tail=50` — look for "Successfully installed
  kafka-python-2.0.2". If pip failed (network blip), restart the
  container: `docker compose restart producer`.
- **`UnknownTopicOrPartition`.** The topic doesn't exist. You skipped
  3a or your shell is pointed at the wrong cluster. `kafka-topics.sh
  --list` to confirm.
- **All messages land on one partition.** You forgot to set the key,
  or the key is `None`. With no key, kafka-python falls back to
  round-robin (modern Kafka) or sticky (older versions); either way,
  the per-card ordering guarantee is gone. Confirm with `producer.send(
  TOPIC, key=row["card_id"], value=row)` — `key` is positional in
  the function signature but explicit is clearer.
- **`AssertionError: Unrecognized configs: {'enable_idempotence': True}`.**
  `kafka-python` (dpkp/kafka-python, the pure-Python lib) doesn't
  support idempotent producers. Drop the `enable_idempotence=True`
  kwarg; you'll get at-least-once with `acks="all"`. If you actually
  need producer-side exactly-once, switch to `confluent-kafka`
  (librdkafka wrapper) — but for our pipeline that semantic is
  Step 7's job (Flink + transactional Kafka sink), not the producer's.
- **Throughput is way below `--rate`.** kafka-python's default
  `linger_ms=0` ack-per-send is the slow path. Set `linger_ms=10` and
  `batch_size=65536` on the producer to batch — you'll easily hit
  10K+ msg/s. (Not strictly needed at our scale; mention it because
  it's the first thing a Kafka user will reach for.)
- **Re-running the producer doubles the topic size.** Expected — the
  topic is append-only. Either accept it (Step 4 doesn't care about
  duplicates) or `kafka-topics.sh --delete --topic transactions` and
  redo 3a + 3d.
- **Kafdrop shows the topic but message count is 0 minutes after the
  producer "finished".** You probably hit `producer.close()` without
  `producer.flush()`, so the in-memory buffer was discarded. Always
  flush.

## What's next

Step 4 stands up `jobs/build_enriched_fact.py` — a Spark **batch**
job that consumes the entire `transactions` topic by offset range,
joins with the four `/curated/*` Parquet dimensions you built in
Step 2, and writes `/analytics/transactions_enriched/`. The Spark
Kafka source is the new concept; the join logic is familiar from
the curate step.

Don't start Step 4 until every check in **Verification** above passes
on a fresh-eyed re-read.
