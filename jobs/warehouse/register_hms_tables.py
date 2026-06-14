"""Register /curated/* and /analytics/* in the Hive Metastore for Presto.

Reads each curated dimension and analytics output from HDFS and writes it
as a Hive table via saveAsTable, which (a) writes Parquet into the Hive
warehouse dir and (b) registers the table in HMS over Thrift in one step.
PrestoDB then sees every table through the hive catalog immediately - the
granular, row-level access pattern that complements Pinot's pre-aggregated
serving.

Submit with the Hive bridge package (not pre-baked into the Spark image,
same posture as the Kafka connector):
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-hive_2.12:3.5.3 \\
        /opt/jobs/warehouse/register_hms_tables.py
"""

from pyspark.sql import SparkSession

CURATED = "hdfs://namenode:9000/curated"
ANALYTICS = "hdfs://namenode:9000/analytics"

# (database, table, source path, partition column or None)
TABLES = [
    ("curated", "customer_profiles", f"{CURATED}/customer-profiles/", None),
    ("curated", "merchant_directory", f"{CURATED}/merchant-directory/", None),
    ("curated", "device_fingerprints", f"{CURATED}/device-fingerprints/", "device_type"),
    ("curated", "fraud_reports", f"{CURATED}/fraud-reports/", "fraud_type"),
    ("analytics", "transactions_enriched", f"{ANALYTICS}/transactions_enriched/", "dt"),
    ("analytics", "customer_features", f"{ANALYTICS}/customer_features/", None),
    ("analytics", "scored", f"{ANALYTICS}/scored/", "dt"),
]


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-register-hms-tables")
             .enableHiveSupport()
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    for db, table, path, part_col in TABLES:
        spark.sql(f"CREATE DATABASE IF NOT EXISTS {db}")
        fqtn = f"{db}.{table}"
        df = spark.read.parquet(path)

        writer = df.write.mode("overwrite").format("parquet")
        if part_col is not None:
            writer = writer.partitionBy(part_col)
        writer.saveAsTable(fqtn)

        count = spark.sql(f"SELECT COUNT(*) FROM {fqtn}").collect()[0][0]
        print(f"  registered {fqtn}: {count} rows"
              + (f" (partitioned by {part_col})" if part_col else ""))

    print("\nDatabases now in HMS:")
    spark.sql("SHOW DATABASES").show(truncate=False)

    spark.stop()


if __name__ == "__main__":
    main()
