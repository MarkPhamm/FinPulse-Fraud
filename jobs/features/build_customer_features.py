"""Build /analytics/customer_features/.

Reads the enriched transaction fact from Step 4, collapses duplicate
transaction IDs from replay/join fan-out, computes one row of behavioral
baseline features per card, and writes Parquet for downstream scoring.

Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/features/build_customer_features.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    approx_count_distinct,
    avg,
    coalesce,
    col,
    collect_set,
    count,
    datediff,
    expr,
    lit,
    max as spark_max,
    min as spark_min,
    stddev_samp,
    when,
)

ENRICHED = "hdfs://namenode:9000/analytics/transactions_enriched/"
CUSTOMERS = "hdfs://namenode:9000/curated/customer-profiles/"
FEATURES = "hdfs://namenode:9000/analytics/customer_features/"


def read_transaction_events(spark: SparkSession):
    """Read enriched rows and keep one event per txn_id before aggregating."""
    return (spark.read.parquet(ENRICHED)
        .select(
            "txn_id",
            "timestamp",
            "card_id",
            "merchant_id",
            "amount",
            "merchant_category",
            "country",
            "channel",
            "is_international",
        )
        .dropDuplicates(["txn_id"]))


def read_customers(spark: SparkSession):
    """Read stable per-card profile fields kept with the feature row."""
    return (spark.read.parquet(CUSTOMERS)
        .select(
            "card_id",
            "customer_id",
            "age",
            "income_bracket",
            "account_age_months",
            "avg_monthly_spend",
            "home_country",
            "typical_categories",
            "credit_limit",
        ))


def build_features(txns):
    """Aggregate transaction history into one behavioral baseline per card."""
    return (txns.groupBy("card_id")
        .agg(
            count("*").alias("txn_count"),
            avg("amount").alias("avg_amount"),
            coalesce(stddev_samp("amount"), lit(0.0)).alias("stddev_amount"),
            expr("percentile_approx(amount, 0.50, 10000)").alias("p50_amount"),
            expr("percentile_approx(amount, 0.95, 10000)").alias("p95_amount"),
            expr("percentile_approx(amount, 0.99, 10000)").alias("p99_amount"),
            approx_count_distinct("merchant_id").alias("unique_merchant_count"),
            approx_count_distinct("country").alias("unique_country_count"),
            approx_count_distinct("merchant_category").alias("unique_category_count"),
            collect_set("country").alias("seen_countries"),
            collect_set("merchant_category").alias("seen_categories"),
            collect_set("channel").alias("seen_channels"),
            avg(col("is_international").cast("double")).alias("pct_intl"),
            avg(when(col("channel") == "online", 1.0).otherwise(0.0)).alias("pct_online"),
            avg(when(col("channel") == "atm", 1.0).otherwise(0.0)).alias("pct_atm"),
            spark_min("timestamp").alias("first_txn_ts"),
            spark_max("timestamp").alias("last_txn_ts"),
        )
        .withColumn("active_days", datediff(col("last_txn_ts"), col("first_txn_ts")) + lit(1))
        .withColumn("txns_per_active_day", col("txn_count") / col("active_days")))


def attach_customer_profile(customers, features):
    """Keep all customer cards, attaching transaction features when present."""
    out = customers.join(features, on="card_id", how="left")
    return out.fillna({
        "txn_count": 0,
        "avg_amount": 0.0,
        "stddev_amount": 0.0,
        "p50_amount": 0.0,
        "p95_amount": 0.0,
        "p99_amount": 0.0,
        "unique_merchant_count": 0,
        "unique_country_count": 0,
        "unique_category_count": 0,
        "pct_intl": 0.0,
        "pct_online": 0.0,
        "pct_atm": 0.0,
        "active_days": 0,
        "txns_per_active_day": 0.0,
    })


def write(out_df) -> None:
    (out_df.coalesce(4).write
        .mode("overwrite")
        .parquet(FEATURES))


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-build-customer-features")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    txns = read_transaction_events(spark)
    customers = read_customers(spark)
    features = build_features(txns)
    out = attach_customer_profile(customers, features)

    write(out)
    print(f"  transaction events used: {txns.count()}")
    print(f"  wrote {out.count()} card feature rows to {FEATURES}")

    spark.stop()


if __name__ == "__main__":
    main()
