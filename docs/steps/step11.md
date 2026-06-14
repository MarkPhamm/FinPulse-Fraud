# Step 11 - Airflow orchestration

This step adds two DAGs:

- **`daily_batch`** - the nightly batch pipeline: curate the four
  dimensions, build the enriched fact, build customer features, run offline
  scoring, gate on a fraud-rate sanity check, then publish to the two
  serving layers (Pinot offline segments + HMS tables for Presto).
- **`streaming_monitor`** - every 15 minutes, check that a Flink job is
  RUNNING, its last checkpoint is recent, and the `transactions-scored`
  topic is producing.

This is the sister doc to
[`docs/plans/plan.md` Section Step 11](../plans/plan.md#step-11--airflow-orchestration)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `airflow/dags/finpulse_lib.py` | Shared helper: run a command in a sibling container via the docker SDK |
| `airflow/dags/daily_batch.py` | Nightly batch DAG (curate -> enrich -> features -> score -> gate -> publish) |
| `airflow/dags/streaming_monitor.py` | 15-minute Flink + Kafka health DAG |
| compose: docker socket mount + `docker` pip | Lets the LocalExecutor drive sibling containers |

## Infrastructure this step had to add

The Airflow LocalExecutor container has **neither Spark nor a Docker CLI**,
so it cannot run `spark-submit` directly. The DAGs drive the sibling
containers (`spark-master`, `pinot-controller`, `kafka`) over the **mounted
Docker socket** using the `docker` Python SDK:

- `airflow-common` now mounts `/var/run/docker.sock` and installs
  `docker==7.1.0` via `_PIP_ADDITIONAL_REQUIREMENTS`.
- `finpulse_lib.run_in(container, cmd)` does `container.exec_run(...)` and
  raises on a non-zero exit, so a failing Spark job fails the task.
- The Pinot controller also mounts `./utils/pinot` at `/opt/pinot-spec` so
  the ingestion job spec is available to the `pinot_offline_ingest` task.

This is the standard "sibling container" orchestration pattern and matches
the plan's intent (`BashOperator` + `docker compose exec`), just via the
SDK instead of a shelled-out CLI.

## What this step does NOT do

- **No SparkSubmitOperator.** That needs Spark on the Airflow PATH; the
  socket + SDK approach is simpler here.
- **No CeleryExecutor.** LocalExecutor forks task subprocesses - fine at
  this scale.
- **No landing task.** Step 1 landing (`hdfs dfs -put` of the raw `.gz`)
  is a one-time bootstrap; `daily_batch` starts from curate, which is
  idempotent and rebuildable from `/landing`.

## Concepts you'll meet here

- **ShortCircuitOperator as a quality gate.** `quality_gate` queries the
  Pinot serving table for the predicted-fraud rate; if it is outside
  `[0.5%, 5%]` it returns False and Airflow skips every downstream publish
  task. This is the "don't publish a broken batch" pattern.
- **Fan-out / fan-in.** The four curate tasks run in parallel; the publish
  stage fans out to `export_pinot_offline -> pinot_offline_ingest` and
  `register_hms_tables` in parallel, then fans back into `publish_metrics`.
- **Monitoring streaming != monitoring batch.** Liveness alone is not
  enough for a streaming job; `streaming_monitor` also checks the
  last-checkpoint age (exactly-once erodes if checkpoints stall) and that
  the output topic is actually growing.

## DAG shape

```text
daily_batch:
  curate_customers ┐
  curate_merchants ├─> build_enriched_fact -> build_customer_features
  curate_devices   │      -> run_offline_scoring -> quality_gate
  curate_fraud_reports ┘                              │
                          ┌──────────────────────────┴───────────┐
                          ▼                                       ▼
              export_pinot_offline -> pinot_offline_ingest   register_hms_tables
                          └──────────────┬────────────────────────┘
                                         ▼
                                   publish_metrics

streaming_monitor (*/15):
  check_flink_running -> check_checkpoint_age
  check_scored_topic
```

## Pre-flight

```sh
# 1. Airflow is up and the docker SDK can reach the socket.
docker compose exec airflow-scheduler python -c "import docker; print(docker.from_env().ping())"
# expect: True

# 2. The DAGs parse with no import errors.
docker compose exec airflow-scheduler airflow dags list-import-errors   # -> No data found
docker compose exec airflow-scheduler airflow dags list | grep finpulse -i
```

If the docker SDK import fails, the Airflow containers were created before
this step - recreate them: `docker compose up -d airflow-scheduler
airflow-webserver` (the entrypoint installs `docker==7.1.0` on start).

## 11a - Test individual tasks

`airflow tasks test` runs one task in-process without scheduling - the
fastest way to validate each step:

```sh
D=2026-06-14
# Streaming monitor (fast, no Spark):
docker compose exec airflow-scheduler airflow tasks test streaming_monitor check_flink_running $D
docker compose exec airflow-scheduler airflow tasks test streaming_monitor check_checkpoint_age $D
docker compose exec airflow-scheduler airflow tasks test streaming_monitor check_scored_topic $D

# Batch quality gate (queries Pinot) and one Spark stage (drives spark-master):
docker compose exec airflow-scheduler airflow tasks test daily_batch quality_gate $D
docker compose exec airflow-scheduler airflow tasks test daily_batch curate_merchants $D
```

## 11b - Run the full batch DAG

```sh
docker compose exec airflow-scheduler airflow dags unpause daily_batch
docker compose exec airflow-scheduler airflow dags trigger daily_batch

# Watch it:
docker compose exec airflow-scheduler airflow dags state daily_batch <run_date>
```

The full run re-executes the heavy Spark stages (enrich ~70s, features
~20s, scoring ~35s, export ~20s) plus the Pinot ingest and HMS
registration - budget several minutes.

## What "passes" looks like

```text
# streaming_monitor
running jobs: ['3820fc29...']
last checkpoint age: 12s (max 300s)
transactions-scored total offset: 1188828

# daily_batch quality_gate
predicted_fraud rate = 0.0379 (band 0.005-0.05)

# daily_batch curate_merchants
[spark-master] exit=0
  read 10000 rows from hdfs://.../merchant-directory.csv.gz
Marking task as SUCCESS. dag_id=daily_batch, task_id=curate_merchants
```

## Troubleshooting

- **`docker.errors.DockerException: ... permission denied`.** The socket is
  not readable by the Airflow user. On Docker Desktop the socket is group
  `root`; the Airflow containers run as `50000:0` (group root) so it works.
  On a host with a `docker` group, add that gid instead.
- **`no RUNNING Flink job`.** The streaming job stopped. Common cause here:
  the taskmanager was OOM-killed (exit 137) under memory pressure, which
  drops all slots and leaves the job `RESTARTING`. Restart it:
  `docker compose up -d flink-taskmanager`, then resubmit the job if
  needed (Step 7). The monitor exists precisely to surface this.
- **`quality_gate` short-circuits.** The Pinot predicted-fraud rate is
  outside the band - inspect `transactions_scored` and the Step 7 rules
  before re-publishing.
- **A Spark task fails with a connector error.** The DAG passes the right
  `--packages` per job (Kafka for enrich, Hive for HMS); a missing one
  means the job path or package constant drifted in `daily_batch.py`.

## What's next

Step 12 is the analysis notebook: read the serving layers (Presto for
granular SQL, Pinot for pre-aggregated trends) and answer the seven
business questions with dollar-impact framing.
