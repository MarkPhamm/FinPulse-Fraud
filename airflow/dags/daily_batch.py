"""daily_batch - the nightly FinPulse batch pipeline.

Chains the Spark batch stages (curate dims -> enrich -> features -> offline
scoring), gates on a fraud-rate sanity check, then publishes the results to
the two serving layers (Pinot offline segments + HMS tables for Presto).

Each task drives a sibling container over the Docker socket (see
finpulse_lib.run_in). Spark jobs run on spark-master; the Pinot ingestion
runs on pinot-controller.
"""

from __future__ import annotations

from datetime import datetime

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator, ShortCircuitOperator

from finpulse_lib import run_in, spark_submit

KAFKA_PKG = "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1"
HIVE_PKG = "org.apache.spark:spark-hive_2.12:3.5.3"

CURATE_JOBS = {
    "curate_customers": "/opt/jobs/curate/curate_customers.py",
    "curate_merchants": "/opt/jobs/curate/curate_merchants.py",
    "curate_devices": "/opt/jobs/curate/curate_devices.py",
    "curate_fraud_reports": "/opt/jobs/curate/curate_fraud_reports.py",
}

FRAUD_RATE_MIN = 0.005
FRAUD_RATE_MAX = 0.05


def _curate(job_path: str):
    run_in("spark-master", spark_submit(job_path))


def _enrich():
    run_in("spark-master", spark_submit(
        "/opt/jobs/enrich/build_enriched_fact.py", packages=KAFKA_PKG))


def _features():
    run_in("spark-master", spark_submit(
        "/opt/jobs/features/build_customer_features.py"))


def _score():
    run_in("spark-master", spark_submit(
        "/opt/jobs/score/score_offline.py"))


def _quality_gate() -> bool:
    """Sanity-check the predicted-fraud rate from the Pinot serving table.

    Returns False (short-circuit) if the rate is outside the expected band,
    which skips the downstream publish tasks.
    """
    sql = (
        "SELECT CAST(SUM(CASE WHEN predicted_fraud THEN 1 ELSE 0 END) AS DOUBLE)"
        " / count(*) FROM transactions_scored"
    )
    r = requests.post("http://pinot-broker:8099/query/sql",
                      json={"sql": sql}, timeout=30)
    r.raise_for_status()
    rate = r.json()["resultTable"]["rows"][0][0]
    print(f"predicted_fraud rate = {rate:.4f} (band {FRAUD_RATE_MIN}-{FRAUD_RATE_MAX})")
    ok = FRAUD_RATE_MIN <= rate <= FRAUD_RATE_MAX
    if not ok:
        print("OUT OF BAND - short-circuiting publish")
    return ok


def _export_pinot():
    run_in("spark-master", spark_submit(
        "/opt/jobs/score/export_pinot_offline.py"))


def _pinot_ingest():
    run_in("pinot-controller", [
        "bin/pinot-admin.sh", "LaunchDataIngestionJob",
        "-jobSpecFile", "/opt/pinot-spec/offline_ingestion_job.yaml",
    ])


def _register_hms():
    run_in("spark-master", spark_submit(
        "/opt/jobs/warehouse/register_hms_tables.py", packages=HIVE_PKG))


def _publish_metrics():
    print("daily_batch published: Pinot offline segments + HMS tables refreshed")


with DAG(
    dag_id="daily_batch",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 * * *",
    catchup=False,
    tags=["finpulse", "batch"],
) as dag:
    curate_tasks = [
        PythonOperator(task_id=name, python_callable=_curate, op_args=[path])
        for name, path in CURATE_JOBS.items()
    ]

    enrich = PythonOperator(task_id="build_enriched_fact", python_callable=_enrich)
    features = PythonOperator(task_id="build_customer_features", python_callable=_features)
    score = PythonOperator(task_id="run_offline_scoring", python_callable=_score)
    gate = ShortCircuitOperator(task_id="quality_gate", python_callable=_quality_gate)
    export_pinot = PythonOperator(task_id="export_pinot_offline", python_callable=_export_pinot)
    pinot_ingest = PythonOperator(task_id="pinot_offline_ingest", python_callable=_pinot_ingest)
    register_hms = PythonOperator(task_id="register_hms_tables", python_callable=_register_hms)
    publish = PythonOperator(task_id="publish_metrics", python_callable=_publish_metrics)

    curate_tasks >> enrich >> features >> score >> gate
    gate >> export_pinot >> pinot_ingest >> publish
    gate >> register_hms >> publish
