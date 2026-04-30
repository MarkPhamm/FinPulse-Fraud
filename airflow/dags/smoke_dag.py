"""Minimal DAG used by `make smoke-airflow` to confirm scheduler + executor work."""

from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator


def _say_hello() -> str:
    return "finpulse smoke ok"


with DAG(
    dag_id="smoke_dag",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["smoke"],
) as dag:
    PythonOperator(task_id="hello_python", python_callable=_say_hello) \
        >> BashOperator(task_id="hello_bash", bash_command="echo finpulse smoke ok")
