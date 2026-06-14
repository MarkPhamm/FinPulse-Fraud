"""Export /analytics/scored/ to local Parquet for Pinot offline ingestion.

Pinot's standalone batch ingestion cannot read HDFS in this stack (no
pinot-hdfs plugin), so Spark writes the scored rows as Parquet to the
shared ./pinot-offline host directory (file:///opt/pinot-offline/scored),
which the pinot-controller container also mounts. The downstream
offline_ingestion_job.yaml then builds segments from that path.

The output columns and types match utils/pinot/transactions_scored.schema.json.
event_time is formatted to the schema's SIMPLE_DATE_FORMAT string; the
offline scored table has no streaming velocity, so velocity_count is 0.

Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/score/export_pinot_offline.py [--date 2025-03-14]
"""

import argparse

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, date_format, lit

SCORED = "hdfs://namenode:9000/analytics/scored/"
OUT = "file:///opt/pinot-offline/scored"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date",
        default=None,
        help="restrict export to a single dt partition (YYYY-MM-DD)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    spark = (SparkSession.builder
             .appName("finpulse-export-pinot-offline")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.parquet(SCORED)
    if args.date:
        df = df.filter(col("dt") == args.date)

    out = df.select(
        "txn_id", "card_id", "merchant_id", "country", "channel",
        "recommended_action",
        col("is_international").cast("boolean").alias("is_international"),
        col("predicted_fraud").cast("boolean").alias("predicted_fraud"),
        col("rule_high_amount").cast("boolean").alias("rule_high_amount"),
        col("rule_velocity").cast("boolean").alias("rule_velocity"),
        col("rule_intl_mismatch").cast("boolean").alias("rule_intl_mismatch"),
        col("rule_high_risk_merchant").cast("boolean").alias("rule_high_risk_merchant"),
        col("amount").cast("double").alias("amount"),
        col("risk_score").cast("int").alias("risk_score"),
        col("merchant_risk_score").cast("int").alias("merchant_risk_score"),
        lit(0).cast("long").alias("velocity_count"),
        date_format(col("timestamp"), "yyyy-MM-dd HH:mm:ss").alias("event_time"),
    ).filter(col("event_time").isNotNull())

    (out.coalesce(1).write
        .mode("overwrite")
        .parquet(OUT))

    print(f"  exported {out.count()} scored rows to {OUT}")

    spark.stop()


if __name__ == "__main__":
    main()
