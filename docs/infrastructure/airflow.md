# Airflow

Orchestrator for the **nightly batch DAG** (Stage 4 of the project
brief — landing → curate → enrich → feature store → score → Pinot
offline segments, with `saveAsTable` registering each `/curated/*`
and `/analytics/*` output in the Hive Metastore so Presto and
Superset see them) and the **streaming-monitoring DAG** (Flink job
liveness + checkpoint age + `fraud-alerts` rate). DAGs live in
[`airflow/dags/`](../../airflow/dags/) and are auto-loaded by the
scheduler.

The Airflow service is intentionally a four-container set (Postgres
metadata DB, one-shot init, long-running webserver, long-running
scheduler) but conceptually a single component.

## Topology

| Service              | Image                               | Hostname             | Host → container ports | What other services talk to it                  |
|----------------------|-------------------------------------|----------------------|------------------------|-------------------------------------------------|
| `postgres`           | `postgres:16-alpine`                | `postgres`           | (no host ports)        | `airflow-init`, `airflow-webserver`, `airflow-scheduler` |
| `airflow-init`       | `apache/airflow:2.10.4-python3.11`  | (default)            | (no host ports)        | One-shot — runs `db migrate` + create-admin, then exits |
| `airflow-webserver`  | `apache/airflow:2.10.4-python3.11`  | `airflow-webserver`  | `8081→8080`            | Browser, scheduler                              |
| `airflow-scheduler`  | `apache/airflow:2.10.4-python3.11`  | `airflow-scheduler`  | (no host ports)        | Postgres; forks task subprocesses               |

Web UI: <http://localhost:8081> — login `admin` / `admin`.

## Configuration source

Airflow shares an `x-airflow-common` YAML anchor across the
init/web/scheduler containers, so all three see the same env vars,
volumes, and user. The non-default knobs:

| Env var                                         | Value                                                      | Why                                                              |
|-------------------------------------------------|------------------------------------------------------------|------------------------------------------------------------------|
| `AIRFLOW__CORE__EXECUTOR`                       | `LocalExecutor`                                            | Forks task subprocesses in the scheduler — no Celery / Redis     |
| `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`           | `postgresql+psycopg2://airflow:airflow@postgres/airflow`   | Postgres metadata DB                                             |
| `AIRFLOW__CORE__LOAD_EXAMPLES`                  | `false`                                                    | No clutter in the UI                                             |
| `AIRFLOW__WEBSERVER__SECRET_KEY`                | `dev-secret-key-change-me`                                 | Dev placeholder — rotate for any non-local use                   |
| `AIRFLOW__API__AUTH_BACKENDS`                   | `basic_auth,session`                                       | Lets the smoke check call `airflow dags trigger` over the REST API |
| `_PIP_ADDITIONAL_REQUIREMENTS`                  | from `.env`                                                | Optional pip add-ons applied at container start                  |

Bind mounts (every Airflow container):

| Host path           | Container path        | Purpose                                                       |
|---------------------|-----------------------|---------------------------------------------------------------|
| `./airflow/dags`    | `/opt/airflow/dags`   | DAG source — auto-discovered by the scheduler                 |
| `./airflow/logs`    | `/opt/airflow/logs`   | Task logs (gitignored)                                        |
| `./airflow/plugins` | `/opt/airflow/plugins`| Custom operators, sensors, hooks                              |
| `./jobs`            | `/opt/jobs:ro`        | Spark job source, read-only — DAGs `spark-submit` against the spark-master container |

`user: "50000:0"` runs all Airflow processes as UID 50000 (the
default Airflow image user) with GID 0, which avoids permission
errors when writing to the bind-mounted `airflow/logs/` directory
on the host. The host UID isn't used because the bind-mount permission
model with GID 0 + the official entrypoint covers it.

## Volumes

| Volume          | Mount path                  | What's persisted                              | Wiped by `make nuke` |
|-----------------|-----------------------------|-----------------------------------------------|----------------------|
| `postgres-data` | `/var/lib/postgresql/data`  | Airflow metadata DB (DAG runs, task instances, logs index, users) | yes |

The webserver and scheduler keep no on-disk state outside Postgres.
Task logs go to the bind-mounted `airflow/logs/` (gitignored), not
a volume — they're easy to inspect from the host.

## Healthcheck

Postgres:

```yaml
test: pg_isready -U airflow
interval: 5s
retries:  10
```

Airflow webserver:

```yaml
test: curl --fail http://localhost:8080/health
interval: 30s
timeout:  10s
retries:  5
```

`airflow-init` is a one-shot (`restart: "no"`) — it has no
healthcheck, but downstream services depend on it via
`condition: service_completed_successfully`, so the webserver and
scheduler only start once `db migrate` + `create admin` finished.

The scheduler has no healthcheck — it uses the same image as
the webserver, so if the web responds, the scheduler is almost
certainly up too. The `make smoke-airflow` target is the integration
probe: it triggers `smoke_dag` and waits for `success`.

## Why this shape

- **`LocalExecutor`, not Celery.** No Redis, no separate worker
  container. The scheduler forks subprocesses for tasks, capped by
  `AIRFLOW__CORE__PARALLELISM`. Fine at this data scale; revisit
  only if task parallelism actually becomes a bottleneck.
- **Init/webserver split.** The official Airflow compose example
  uses the same pattern. The init container runs `db migrate` and
  `create-admin` exactly once; the long-lived web/scheduler depend
  on it via `service_completed_successfully` so they never start
  against an unmigrated DB. `restart: "no"` on init keeps it from
  re-running on `docker compose up`.
- **Postgres, not SQLite.** SQLite works for a single-process
  scheduler but breaks the moment two threads write to the
  metadata DB at once. With `LocalExecutor` plus parallel DAG
  parsing, you'd hit *database is locked* errors immediately.
  Postgres is one extra container; it's worth it.
- **Webserver port is remapped 8081 ← 8080.** Container :8080
  collides with the Spark master UI on the host. Airflow gets
  :8081, Spark master keeps :8080.
- **Dev secret key in plaintext.** This is intentional for a class
  project — the alternative (a `.env`-loaded secret) only adds
  ceremony. Don't reuse this stack as-is for anything multi-tenant.

## Alternatives

Where Airflow sits in the orchestration landscape:

| System              | Lineage                          | Pick instead when…                                                                                                    |
|---------------------|----------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| **Dagster**         | Asset-centric orchestrator (2018+) | You want to declare *what data assets exist* and have the framework derive the DAG. Better types, better local dev, better observability. The strongest 2026 alternative for new projects. |
| **Prefect**         | Pythonic orchestrator            | You want dynamic DAGs at runtime (DAG shape computed from data) and simpler retry semantics. v2 is solid.             |
| **Argo Workflows**  | Kubernetes-native                | You're on Kubernetes already and want declarative YAML DAGs that run as pods. Common in MLOps.                        |
| **Luigi**           | Spotify's older orchestrator     | You want a tiny dependency, no scheduler, cron-the-workers-yourself. Largely obsolete.                                |
| **Cron + bash**     | The baseline                     | 1–2 simple jobs, no dependencies, no retries needed. Stops working the moment you need DAG semantics.                 |
| **Mage / Kestra**   | Newer entrants                   | You're trying out modern orchestrators with stronger DX (notebook-style UIs, live state).                             |
| **dbt-core scheduler** | SQL-native                    | All your transformations are SQL on a warehouse. Doesn't replace Airflow for non-SQL work.                            |

Airflow's strength is **incumbency** — every vendor has an
integration, every data engineer knows it, every cloud has a
managed offering. Dagster is gaining ground for greenfield projects
where the data-asset model fits naturally.

## Common commands

Trigger a DAG from the host:

```sh
docker compose exec airflow-webserver airflow dags trigger smoke_dag
docker compose exec airflow-webserver airflow dags list-runs -d smoke_dag
```

Pause / unpause:

```sh
docker compose exec airflow-webserver airflow dags pause smoke_dag
docker compose exec airflow-webserver airflow dags unpause smoke_dag
```

Inspect task logs from the host (no `exec` needed — they're bind-mounted):

```sh
ls airflow/logs/dag_id=smoke_dag/
```

```sh
make smoke-airflow   # trigger smoke_dag and wait for success
```

UIs:

- Airflow web: <http://localhost:8081> — `admin` / `admin`

## Caveats

- **Postgres is shared with nothing else.** Pinot uses ZooKeeper,
  Superset uses SQLite — neither lands in this Postgres. If you
  ever switch Superset's metadata DB to Postgres (an option called
  out in [`superset.md`](superset.md)), create a separate
  `superset` database in this same instance, not a separate
  cluster.
- **`make nuke` wipes all DAG history.** Run history, task
  instance state, XCom values — all in `postgres-data`. The DAGs
  themselves live in `airflow/dags/` on the host and survive.
- **DAG parsing is sensitive to import errors.** A typo in a DAG
  file shows up in the *DAG Import Errors* banner at the top of
  the Airflow UI but won't crash the scheduler. Watch the banner
  after editing.
- **Scaling out tasks needs a bigger executor.** With
  `LocalExecutor`, total parallelism is capped at one machine.
  When the nightly DAG starts running 4+ Spark steps in parallel,
  switch to CeleryExecutor (adds Redis + workers) or
  KubernetesExecutor (out of scope for class).
