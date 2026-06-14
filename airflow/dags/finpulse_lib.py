"""Shared helpers for the FinPulse Airflow DAGs.

The Airflow LocalExecutor has neither Spark nor the Pinot/HDFS CLIs, so the
DAGs drive the sibling containers over the mounted Docker socket using the
docker SDK. This module is not a DAG (no DAG object), it is just imported by
the DAG files (the dags/ folder is on sys.path).
"""

from __future__ import annotations

import docker

BOOTSTRAP_IN_NETWORK = "kafka:9094"


def run_in(container_name: str, command, *, log_tail: int = 40) -> str:
    """Exec a command in a running container; raise on non-zero exit.

    `command` may be a list (preferred, no shell) or a string. Output is
    captured, the tail is printed to the task log, and a RuntimeError is
    raised if the command fails.
    """
    client = docker.from_env()
    container = client.containers.get(container_name)
    exit_code, output = container.exec_run(command, demux=False)
    text = output.decode("utf-8", errors="replace") if output else ""

    lines = text.splitlines()
    tail = "\n".join(lines[-log_tail:]) if len(lines) > log_tail else text
    print(f"[{container_name}] exit={exit_code}\n{tail}")

    if exit_code != 0:
        raise RuntimeError(
            f"command in {container_name} exited {exit_code}: {command}"
        )
    return text


def spark_submit(job_path: str, packages: str | None = None) -> list[str]:
    """Build a spark-submit argv for the standalone master."""
    cmd = [
        "/opt/spark/bin/spark-submit",
        "--master", "spark://spark-master:7077",
    ]
    if packages:
        cmd += ["--packages", packages]
    cmd.append(job_path)
    return cmd
