"""Print offline scoring metrics from /analytics/scored/.

Step 6 verifier: confusion matrix, precision/recall/F1, rule trigger
rates, and a prevented-loss estimate.

Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/score/check_offline_scores.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import avg, col, count, sum as spark_sum, when

SCORED = "hdfs://namenode:9000/analytics/scored/"

RULE_COLS = [
    "rule_high_amount",
    "rule_velocity",
    "rule_intl_mismatch",
    "rule_unknown_device_vpn",
    "rule_high_risk_merchant",
]


def print_confusion_and_metrics(df) -> None:
    agg = df.agg(
        count("*").alias("total"),
        spark_sum(
            when(col("predicted_fraud") & col("confirmed_fraud"), 1).otherwise(0)
        ).alias("tp"),
        spark_sum(
            when(col("predicted_fraud") & ~col("confirmed_fraud"), 1).otherwise(0)
        ).alias("fp"),
        spark_sum(
            when(~col("predicted_fraud") & col("confirmed_fraud"), 1).otherwise(0)
        ).alias("fn"),
        spark_sum(
            when(~col("predicted_fraud") & ~col("confirmed_fraud"), 1).otherwise(0)
        ).alias("tn"),
        avg(col("risk_score").cast("double")).alias("avg_risk_score"),
        spark_sum(col("predicted_fraud").cast("long")).alias("predicted_fraud_rows"),
        spark_sum(col("confirmed_fraud").cast("long")).alias("confirmed_fraud_rows"),
    ).collect()[0]

    tp = agg["tp"]
    fp = agg["fp"]
    fn = agg["fn"]
    tn = agg["tn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    avg_fraud_amount = (
        df.filter(col("confirmed_fraud"))
        .agg({"amount": "avg"})
        .collect()[0][0]
        or 0.0
    )

    print("Scored transaction summary:")
    (df.agg(
        count("*").alias("rows"),
        avg("risk_score").alias("avg_risk_score"),
        spark_sum(col("predicted_fraud").cast("long")).alias("predicted_fraud_rows"),
        spark_sum(col("confirmed_fraud").cast("long")).alias("confirmed_fraud_rows"),
    ).show(truncate=False))

    print("Confusion matrix:")
    print(f"  TP={tp}  FP={fp}")
    print(f"  FN={fn}  TN={tn}")
    print(f"  precision={precision:.4f}")
    print(f"  recall={recall:.4f}")
    print(f"  f1={f1:.4f}")
    print(f"  prevented_loss_estimate={(tp * avg_fraud_amount):,.2f}")


def print_rule_rates(df) -> None:
    print("Rule trigger rates:")
    exprs = [
        avg(col(rule).cast("double")).alias(rule)
        for rule in RULE_COLS
    ]
    df.agg(*exprs).show(truncate=False)

    print("Risk score distribution:")
    df.groupBy("risk_score").count().orderBy("risk_score").show()


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-check-offline-scores")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    df = spark.read.parquet(SCORED)
    print_confusion_and_metrics(df)
    print_rule_rates(df)

    print("Sample scored rows:")
    df.select(
        "txn_id",
        "card_id",
        "amount",
        "risk_score",
        "predicted_fraud",
        "confirmed_fraud",
        "recommended_action",
        "rule_high_amount",
        "rule_velocity",
        "rule_intl_mismatch",
        "rule_unknown_device_vpn",
        "rule_high_risk_merchant",
    ).filter(col("predicted_fraud")).show(5, truncate=False)

    spark.stop()


if __name__ == "__main__":
    main()
