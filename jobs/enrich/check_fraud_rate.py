"""Print the global confirmed_fraud rate from /analytics/transactions_enriched/.

Step 4 rubric check - expects ~0.0127 on the generated seed data.
Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/enrich/check_fraud_rate.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import avg, col, count, sum as spark_sum

ANALYTICS = "hdfs://namenode:9000/analytics/transactions_enriched/"


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-check-fraud-rate")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.parquet(ANALYTICS)
    (df.agg(
        count("*").alias("total"),
        spark_sum(col("confirmed_fraud").cast("long")).alias("fraud_rows"),
        avg(col("confirmed_fraud").cast("double")).alias("fraud_rate"),
    ).show(truncate=False))

    spark.stop()


if __name__ == "__main__":
    main()
