"""Curate fraud-reports: gzip JSON at /landing → Parquet at /curated.

Big dimension (~15K rows) — partitioned by fraud_type.
"""

from pyspark.sql import SparkSession

LANDING = "hdfs://namenode:9000/landing/fraud-reports/fraud-reports.json.gz"
CURATED = "hdfs://namenode:9000/curated/fraud-reports/"


def main() -> None:
    spark = SparkSession.builder.appName("finpulse-curate-fraud-reports").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.option("multiLine", "true").json(LANDING)

    print(f"  read {df.count()} rows from {LANDING}")
    df.printSchema()

    df.write.mode("overwrite").partitionBy("fraud_type").parquet(CURATED)
    print(f"  wrote Parquet to {CURATED}")

    spark.stop()


if __name__ == "__main__":
    main()
