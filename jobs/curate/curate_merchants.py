"""Curate merchant-directory: gzip CSV at /landing → Parquet at /curated.

Small dimension (~10K rows) — no partitioning.
"""

from pyspark.sql import SparkSession

LANDING = "hdfs://namenode:9000/landing/merchant-directory/merchant-directory.csv.gz"
CURATED = "hdfs://namenode:9000/curated/merchant-directory/"


def main() -> None:
    spark = SparkSession.builder.appName("finpulse-curate-merchants").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.option("header", "true").option("inferSchema", "true").csv(LANDING)

    print(f"  read {df.count()} rows from {LANDING}")
    df.printSchema()

    df.write.mode("overwrite").parquet(CURATED)
    print(f"  wrote Parquet to {CURATED}")

    spark.stop()


if __name__ == "__main__":
    main()
