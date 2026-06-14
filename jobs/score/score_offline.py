"""Score transactions offline with rule-based fraud detection.

Reads the enriched fact and customer feature store, applies five fraud
rules, writes per-transaction predictions to /analytics/scored/, and
prints imbalance-aware metrics against confirmed_fraud.

Submit:
    docker compose exec spark-master /opt/spark/bin/spark-submit \\
        --master spark://spark-master:7077 \\
        /opt/jobs/score/score_offline.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    array_contains,
    coalesce,
    col,
    count,
    lit,
    size,
    sum as spark_sum,
    unix_timestamp,
    when,
)
from pyspark.sql.window import Window

ENRICHED = "hdfs://namenode:9000/analytics/transactions_enriched/"
FEATURES = "hdfs://namenode:9000/analytics/customer_features/"
SCORED = "hdfs://namenode:9000/analytics/scored/"
RISK_THRESHOLD = 2


def read_scoring_inputs(spark: SparkSession):
    """Load one enriched row per txn and join customer baselines."""
    txns = (spark.read.parquet(ENRICHED)
        .select(
            "txn_id",
            "timestamp",
            "card_id",
            "merchant_id",
            "amount",
            "country",
            "channel",
            "is_international",
            "is_vpn",
            "is_known_device",
            col("risk_score").alias("merchant_risk_score"),
            "confirmed_fraud",
            "dt",
        )
        .dropDuplicates(["txn_id"]))

    features = (spark.read.parquet(FEATURES)
        .select(
            "card_id",
            "avg_monthly_spend",
            "home_country",
            "seen_countries",
            "unique_country_count",
        ))

    return txns.join(features, on="card_id", how="left")


def apply_rules(df):
    """Compute the five brief rules plus aggregate risk score."""
    velocity_window = (
        Window.partitionBy("card_id")
        .orderBy(unix_timestamp("timestamp"))
        .rangeBetween(-600, 0)
    )

    with_velocity = df.withColumn(
        "velocity_count",
        count("*").over(velocity_window),
    )

    return (with_velocity
        .withColumn(
            "rule_high_amount",
            col("amount") > (lit(3.0) * col("avg_monthly_spend") / lit(30.0)),
        )
        .withColumn("rule_velocity", col("velocity_count") >= lit(5))
        .withColumn(
            "rule_intl_mismatch",
            col("is_international")
            & (size(col("seen_countries")) == 1)
            & array_contains(col("seen_countries"), col("home_country")),
        )
        .withColumn(
            "rule_unknown_device_vpn",
            (~coalesce(col("is_known_device"), lit(False)))
            & coalesce(col("is_vpn"), lit(False)),
        )
        .withColumn(
            "rule_high_risk_merchant",
            coalesce(col("merchant_risk_score"), lit(0)) >= lit(8),
        )
        .withColumn(
            "risk_score",
            col("rule_high_amount").cast("int")
            + col("rule_velocity").cast("int")
            + col("rule_intl_mismatch").cast("int")
            + col("rule_unknown_device_vpn").cast("int")
            + col("rule_high_risk_merchant").cast("int"),
        )
        .withColumn("predicted_fraud", col("risk_score") >= lit(RISK_THRESHOLD))
        .withColumn(
            "recommended_action",
            when(col("risk_score") >= lit(3), lit("block"))
            .when(col("risk_score") >= lit(RISK_THRESHOLD), lit("review"))
            .otherwise(lit("approve")),
        ))


def select_output(df):
    """Keep scoring columns plus the label for downstream evaluation."""
    return df.select(
        "txn_id",
        "timestamp",
        "card_id",
        "merchant_id",
        "amount",
        "country",
        "channel",
        "is_international",
        "merchant_risk_score",
        "confirmed_fraud",
        "dt",
        "rule_high_amount",
        "rule_velocity",
        "rule_intl_mismatch",
        "rule_unknown_device_vpn",
        "rule_high_risk_merchant",
        "risk_score",
        "predicted_fraud",
        "recommended_action",
    )


def write(scored_df) -> None:
    (scored_df.write
        .mode("overwrite")
        .partitionBy("dt")
        .parquet(SCORED))


def print_metrics(scored_df) -> None:
    """Print confusion matrix and imbalance-aware metrics."""
    agg = scored_df.agg(
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
        spark_sum(col("confirmed_fraud").cast("long")).alias("confirmed_fraud_rows"),
        spark_sum(col("predicted_fraud").cast("long")).alias("predicted_fraud_rows"),
    ).collect()[0]

    tp = agg["tp"]
    fp = agg["fp"]
    fn = agg["fn"]
    tn = agg["tn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    avg_fraud_amount = (
        scored_df.filter(col("confirmed_fraud"))
        .agg({"amount": "avg"})
        .collect()[0][0]
        or 0.0
    )
    prevented_loss = tp * avg_fraud_amount

    print("\nConfusion matrix:")
    print(f"  TP={tp}  FP={fp}")
    print(f"  FN={fn}  TN={tn}")
    print("\nMetrics:")
    print(f"  precision={precision:.4f}")
    print(f"  recall={recall:.4f}")
    print(f"  f1={f1:.4f}")
    print(f"  predicted_fraud_rows={agg['predicted_fraud_rows']}")
    print(f"  confirmed_fraud_rows={agg['confirmed_fraud_rows']}")
    print("\nBusiness impact:")
    print(f"  avg_confirmed_fraud_amount={avg_fraud_amount:.2f}")
    print(f"  prevented_loss_estimate={prevented_loss:,.2f}")


def main() -> None:
    spark = (SparkSession.builder
             .appName("finpulse-score-offline")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")

    joined = read_scoring_inputs(spark)
    scored = select_output(apply_rules(joined))

    write(scored)
    print(f"  wrote {scored.count()} scored rows to {SCORED}")
    print_metrics(scored)

    spark.stop()


if __name__ == "__main__":
    main()
