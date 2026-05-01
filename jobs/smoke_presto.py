"""
Smoke job for the Spark <-> HMS <-> Presto chain.

Creates a tiny DataFrame, writes it as a Hive table via saveAsTable. Spark:
  (a) writes Parquet bytes to hdfs://namenode:9000/warehouse/smoke_hms/
  (b) registers default.smoke_hms in HMS over Thrift.

Presto then sees the table immediately via the hive catalog. Used by
`make smoke-presto`.

Submit with the spark-hive package — the apache/spark image does NOT bundle
the Hive bridge JAR (same posture as the spark-sql-kafka connector):

    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-hive_2.12:3.5.3 \\
        /opt/jobs/smoke_presto.py
"""

from pyspark.sql import SparkSession

spark = (
    SparkSession.builder
    .appName("smoke_presto")
    .enableHiveSupport()
    .getOrCreate()
)

rows = [(1, "alice", 100.0), (2, "bob", 250.0), (3, "carol", 42.0)]
df = spark.createDataFrame(rows, schema="id INT, name STRING, amount DOUBLE")

spark.sql("CREATE DATABASE IF NOT EXISTS default")
df.write.mode("overwrite").saveAsTable("default.smoke_hms")

count = spark.sql("SELECT COUNT(*) FROM default.smoke_hms").collect()[0][0]
print(f"smoke_hms row count: {count}")
assert count == 3, f"expected 3 rows, got {count}"
print("smoke_presto OK")

spark.stop()
