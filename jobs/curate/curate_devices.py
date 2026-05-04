"""Curate merchant-directory: gzip CSV at /landing → Parquet at /curated.

Big dimension (~600K rows) — partitioned by device_type.
"""

from pyspark.sql import SparkSession

LANDING = "hdfs://namenode:9000/landing/device-fingerprints/device-fingerprints.csv.gz"
CURATED = "hdfs://namenode:9000/curated/device-fingerprints/"


def main() -> None:
    spark = SparkSession.builder.appName("finpulse-curate-devices").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.option("header", "true").option("inferSchema", "true").csv(LANDING)

    print(f"  read {df.count()} rows from {LANDING}")
    df.printSchema()

    df.write.mode("overwrite").partitionBy("device_type").parquet(CURATED)
    print(f"  wrote Parquet to {CURATED}")

    spark.stop()


if __name__ == "__main__":
    main()
