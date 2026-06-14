"""Build /analytics/transactions_enriched/.

Reads the entire `transactions` Kafka topic in batch mode, parses the
JSON `value` column into typed columns, joins with the four /curated/
dimensions, attaches a confirmed_fraud label, and writes Parquet
partitioned by date.

Submit (note --packages for the Kafka connector — it is NOT pre-baked
into the Spark image):

    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1 \\
        /opt/jobs/enrich/build_enriched_fact.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import broadcast, col, from_json, to_date, when
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    StringType,
    StructField,
    StructType,
)

BOOTSTRAP = "kafka:9094"
TOPIC = "transactions"
CURATED = "hdfs://namenode:9000/curated"
ANALYTICS = "hdfs://namenode:9000/analytics/transactions_enriched/"

# Inside-the-message JSON schema — see Step 3 producer.
TXN_SCHEMA = StructType([
    StructField("txn_id", StringType()),
    StructField("timestamp", StringType()),
    StructField("card_id", StringType()),
    StructField("merchant_id", StringType()),
    StructField("amount", StringType()),
    StructField("currency", StringType()),
    StructField("merchant_category", StringType()),
    StructField("country", StringType()),
    StructField("channel", StringType()),
    StructField("is_international", StringType()),
])


def read_transactions(spark: SparkSession):
    """Batch-read the whole `transactions` topic and return typed rows."""
    raw = (spark.read.format("kafka")
           .option("kafka.bootstrap.servers", BOOTSTRAP)
           .option("subscribe", TOPIC)
           .option("startingOffsets", "earliest")
           .option("endingOffsets", "latest")
           .load())

    return (raw
        .select(from_json(col("value").cast("string"), TXN_SCHEMA).alias("t"))
        .select("t.*")
        .withColumn("amount", col("amount").cast(DoubleType()))
        .withColumn("is_international", col("is_international").cast(BooleanType()))
        .withColumn("timestamp", col("timestamp").cast("timestamp")))


def read_dims(spark: SparkSession):
    """Read the four curated dim tables, projecting only kept columns."""
    customers = (spark.read.parquet(f"{CURATED}/customer-profiles/")
        .select(
            "card_id", "age", "income_bracket", "account_age_months",
            "avg_monthly_spend", "home_country", "typical_categories",
            "credit_limit",
        ))

    merchants = (spark.read.parquet(f"{CURATED}/merchant-directory/")
        .select(
            "merchant_id",
            col("name").alias("merchant_name"),
            col("country").alias("merchant_country"),
            "risk_score", "avg_transaction_amount",
        ))

    devices = (spark.read.parquet(f"{CURATED}/device-fingerprints/")
        .select(
            "txn_id", "device_type", "os", "browser",
            "ip_country", "is_vpn", "is_known_device",
        ))

    fraud_reports = (spark.read.parquet(f"{CURATED}/fraud-reports/")
        .select(
            "txn_id", "fraud_type", "resolution", "amount_disputed",
        ))

    return customers, merchants, devices, fraud_reports


def enrich(txns, customers, merchants, devices, fraud_reports):
    """Join txns with the four dims. Broadcast the two small ones."""
    return (txns
        .join(broadcast(customers), on="card_id", how="inner")
        .join(broadcast(merchants), on="merchant_id", how="inner")
        .join(devices, on="txn_id", how="left")
        .join(fraud_reports, on="txn_id", how="left"))


def label_and_partition(enriched):
    """Attach confirmed_fraud + dt; drop the report columns we don't keep."""
    return (enriched
        .withColumn(
            "confirmed_fraud",
            when(col("resolution") == "confirmed_fraud", True).otherwise(False),
        )
        .withColumn("dt", to_date(col("timestamp"))))


def write(out_df) -> None:
    (out_df.write
        .mode("overwrite")
        .partitionBy("dt")
        .parquet(ANALYTICS))


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-build-enriched-fact")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    txns = read_transactions(spark)
    customers, merchants, devices, fraud_reports = read_dims(spark)
    enriched = enrich(txns, customers, merchants, devices, fraud_reports)
    out = label_and_partition(enriched)

    write(out)
    print(f"  wrote {out.count()} rows to {ANALYTICS}")

    spark.stop()


if __name__ == "__main__":
    main()
