"""Publish /analytics/customer_features/ to the Kafka topic customer-features.

The Flink streaming scorer (Step 7) joins each transaction against the
customer baseline. Rather than read Parquet from HDFS inside Flink, we
publish the small feature table to a compacted Kafka topic keyed by
card_id, which Flink consumes as an upsert-kafka (versioned) table.

The Kafka message key is the card_id; the value is JSON of the baseline
fields the streaming rules need (key field excluded, matching the Flink
upsert-kafka 'value.fields-include' = 'EXCEPT_KEY' setting).

Submit (note --packages for the Kafka connector):
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \\
        /opt/jobs/features/publish_customer_features.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, struct, to_json

FEATURES = "hdfs://namenode:9000/analytics/customer_features/"
BOOTSTRAP = "kafka:9094"
TOPIC = "customer-features"


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-publish-customer-features")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    features = spark.read.parquet(FEATURES)

    payload = (features.select(
        col("card_id").cast("string").alias("key"),
        to_json(struct(
            col("avg_monthly_spend"),
            col("home_country"),
            col("seen_countries"),
            col("unique_country_count"),
        )).alias("value"),
    ))

    (payload.write
        .format("kafka")
        .option("kafka.bootstrap.servers", BOOTSTRAP)
        .option("topic", TOPIC)
        .save())

    print(f"  published {payload.count()} card feature rows to topic '{TOPIC}'")

    spark.stop()


if __name__ == "__main__":
    main()
