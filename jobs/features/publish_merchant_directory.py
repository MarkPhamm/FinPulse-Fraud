"""Publish /curated/merchant-directory/ risk scores to Kafka topic merchant-directory.

The Flink streaming scorer (Step 7) needs each merchant's risk_score to
evaluate the high-risk-merchant rule. Like the customer baseline, we ship
the small merchant dimension to a compacted Kafka topic keyed by
merchant_id, which Flink consumes as an upsert-kafka (versioned) table.

The value JSON field is named merchant_risk_score to avoid colliding with
the scorer's own risk_score output column.

Submit (note --packages for the Kafka connector):
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \\
        /opt/jobs/features/publish_merchant_directory.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, struct, to_json

MERCHANTS = "hdfs://namenode:9000/curated/merchant-directory/"
BOOTSTRAP = "kafka:9094"
TOPIC = "merchant-directory"


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-publish-merchant-directory")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    merchants = spark.read.parquet(MERCHANTS)

    payload = (merchants.select(
        col("merchant_id").cast("string").alias("key"),
        to_json(struct(
            col("risk_score").cast("int").alias("merchant_risk_score"),
        )).alias("value"),
    ))

    (payload.write
        .format("kafka")
        .option("kafka.bootstrap.servers", BOOTSTRAP)
        .option("topic", TOPIC)
        .save())

    print(f"  published {payload.count()} merchant rows to topic '{TOPIC}'")

    spark.stop()


if __name__ == "__main__":
    main()
