"""Print sanity checks for /analytics/customer_features/.

Step 5 verifier: confirms one feature row per card and shows the main
behavioral baseline distributions.

Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/features/check_customer_features.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import avg, col, count, countDistinct, max as spark_max

FEATURES = "hdfs://namenode:9000/analytics/customer_features/"


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-check-customer-features")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.parquet(FEATURES)

    print("Feature store row checks:")
    (df.agg(
        count("*").alias("rows"),
        countDistinct("card_id").alias("distinct_cards"),
        spark_max("txn_count").alias("max_txn_count"),
        avg("txn_count").alias("avg_txn_count"),
        avg("avg_amount").alias("avg_of_avg_amount"),
        avg("pct_intl").alias("avg_pct_intl"),
        avg("pct_online").alias("avg_pct_online"),
    ).show(truncate=False))

    print("Duplicate card_id rows; expect no output:")
    (df.groupBy("card_id")
       .count()
       .filter(col("count") > 1)
       .show(10, truncate=False))

    print("Amount and count distributions:")
    df.select(
        "txn_count",
        "avg_amount",
        "stddev_amount",
        "p95_amount",
        "unique_merchant_count",
        "unique_country_count",
        "pct_intl",
        "pct_online",
    ).describe().show(truncate=False)

    print("Sample feature rows:")
    df.select(
        "card_id",
        "txn_count",
        "avg_amount",
        "p95_amount",
        "unique_merchant_count",
        "seen_countries",
        "pct_intl",
        "pct_online",
    ).show(5, truncate=False)

    spark.stop()


if __name__ == "__main__":
    main()
