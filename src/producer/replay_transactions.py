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

CSV_PATH = "/data/transactions.csv.gz"  # bind-mounted from ./data
TOPIC = "transactions"
BOOTSTRAP = "kafka:9094"  # internal listener (we're inside the network)


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
