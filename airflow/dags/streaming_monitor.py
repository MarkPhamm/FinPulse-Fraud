"""streaming_monitor - liveness + health checks for the Flink streaming layer.

Runs every 15 minutes. Three checks:
  1. A Flink job is RUNNING.
  2. Its last completed checkpoint is recent (exactly-once is not eroding).
  3. The transactions-scored topic is non-empty (the scorer is producing).

Any failed check raises, so the task goes red and Airflow surfaces the
alert. Flink is reached over its REST API; Kafka offsets via a CLI exec.
"""

from __future__ import annotations

import time
from datetime import datetime

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator

from finpulse_lib import run_in

FLINK = "http://flink-jobmanager:8081"
MAX_CHECKPOINT_AGE_S = 300


def _check_flink_running() -> str:
    jobs = requests.get(f"{FLINK}/jobs", timeout=30).json()["jobs"]
    running = [j["id"] for j in jobs if j["status"] == "RUNNING"]
    print(f"running jobs: {running}")
    if not running:
        raise RuntimeError(f"no RUNNING Flink job (states: {jobs})")
    return running[0]


def _check_checkpoint_age() -> None:
    job_id = _check_flink_running()
    cp = requests.get(f"{FLINK}/jobs/{job_id}/checkpoints", timeout=30).json()
    latest = (cp.get("latest") or {}).get("completed")
    if not latest:
        print("no completed checkpoint yet (job may have just started)")
        return
    age_s = time.time() - latest["latest_ack_timestamp"] / 1000.0
    print(f"last checkpoint age: {age_s:.0f}s (max {MAX_CHECKPOINT_AGE_S}s)")
    if age_s > MAX_CHECKPOINT_AGE_S:
        raise RuntimeError(f"checkpoint stale: {age_s:.0f}s > {MAX_CHECKPOINT_AGE_S}s")


def _check_scored_topic() -> None:
    out = run_in("kafka", [
        "/opt/kafka/bin/kafka-get-offsets.sh",
        "--bootstrap-server", "kafka:9094",
        "--topic", "transactions-scored",
    ])
    total = sum(int(line.rsplit(":", 1)[1]) for line in out.splitlines() if ":" in line)
    print(f"transactions-scored total offset: {total}")
    if total <= 0:
        raise RuntimeError("transactions-scored is empty - scorer not producing")


with DAG(
    dag_id="streaming_monitor",
    start_date=datetime(2026, 1, 1),
    schedule="*/15 * * * *",
    catchup=False,
    max_active_runs=1,
    tags=["finpulse", "monitor"],
) as dag:
    flink_live = PythonOperator(
        task_id="check_flink_running", python_callable=_check_flink_running)
    checkpoint_age = PythonOperator(
        task_id="check_checkpoint_age", python_callable=_check_checkpoint_age)
    scored_topic = PythonOperator(
        task_id="check_scored_topic", python_callable=_check_scored_topic)

    flink_live >> checkpoint_age
    scored_topic
