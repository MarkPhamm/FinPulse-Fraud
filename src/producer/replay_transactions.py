"""Replay data/transactions.csv.gz into Kafka topic 'transactions'.

Each CSV row becomes one Kafka message:
  - key   = card_id (bytes) — same card → same partition, ordering preserved
  - value = JSON-encoded row (bytes)

Run from inside the producer container:
    docker compose exec producer python /opt/producer/replay_transactions.py
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import time

from kafka import KafkaProducer

CSV_PATH = "/data/transactions.csv.gz"  # bind-mounted from ./data
TOPIC = "transactions"
BOOTSTRAP = "kafka:9094"  # internal listener (we're inside the network)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--rate",
        type=int,
        default=200,
        help="max messages per second (default: 200)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N messages (default: send all rows)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: k.encode("utf-8"),
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks="all",
    )

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
                print(
                    f"  sent {sent} rows in {elapsed:.1f}s "
                    f"({sent / elapsed:.0f} msg/s)"
                )

    producer.flush()
    producer.close()
    elapsed = time.monotonic() - start
    print(
        f"\nDone. Sent {sent} rows to topic '{TOPIC}' "
        f"in {elapsed:.1f}s ({sent / elapsed:.0f} msg/s)."
    )


if __name__ == "__main__":
    main()
