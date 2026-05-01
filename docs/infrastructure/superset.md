# Superset

Apache Superset is the BI / dashboard UI on top of [Pinot](pinot.md)
**and** [PrestoDB](presto.md). It connects to both via SQLAlchemy
drivers (`pinotdb` for Pinot, `pyhive[presto]` for Presto), installed
at container startup by bind-mounting `requirements-local.txt` at the
path the official entrypoint already pip-installs from. This
sidesteps the need for a custom Dockerfile.

The two-container split (`superset-init` one-shot + `superset`
long-running) mirrors the [Airflow](airflow.md) pattern.

## Topology

| Service          | Image                      | Hostname    | Host → container ports | What other services talk to it                                |
|------------------|----------------------------|-------------|------------------------|---------------------------------------------------------------|
| `superset-init`  | `apache/superset:4.1.1`    | (default)   | (no host ports)        | One-shot — runs db upgrade, create-admin, init, then exits    |
| `superset`       | `apache/superset:4.1.1`    | `superset`  | `8088→8088`            | Browser; outbound to `pinot-broker:8099` for SQL              |

Web UI: <http://localhost:8088> — login `admin` / `admin`.

## Configuration source

Superset reads three things on startup, all bind-mounted into both
containers:

| Host file                                                                          | Container path                          | Purpose                                                  |
|------------------------------------------------------------------------------------|-----------------------------------------|----------------------------------------------------------|
| [`docker/superset/superset_config.py`](../../docker/superset/superset_config.py)   | `/app/pythonpath/superset_config.py`    | Metadata DB URI, secret key, feature flags               |
| [`docker/superset/requirements-local.txt`](../../docker/superset/requirements-local.txt) | `/app/docker/requirements-local.txt`    | `pinotdb` (and any other extras) — pip-installed at boot |
| [`docker/superset/superset-init.sh`](../../docker/superset/superset-init.sh)       | `/app/superset-init.sh` (init container) | One-shot: pip install + db upgrade + create-admin + init |
| [`docker/superset/superset-bootstrap.sh`](../../docker/superset/superset-bootstrap.sh) | `/app/superset-bootstrap.sh` (long-running) | pip install + exec the stock `run-server.sh`         |

Superset's entrypoint adds `/app/pythonpath` to `PYTHONPATH` and
imports `superset_config`, which overrides defaults from the
upstream `superset.config` module. The non-default settings:

| Setting                     | Value                                              | Why                                                              |
|-----------------------------|----------------------------------------------------|------------------------------------------------------------------|
| `SQLALCHEMY_DATABASE_URI`   | `sqlite:////app/superset_home/superset.db`         | SQLite metadata DB on the `superset-data` named volume           |
| `SECRET_KEY`                | from `SUPERSET_SECRET_KEY` env (`dev-secret-change-me`) | Required by Flask. Dev only — rotate for any non-local use  |
| `ENABLE_PROXY_FIX`          | `False`                                            | No reverse proxy in this setup; silences a startup warning       |
| `FEATURE_FLAGS`             | `{}`                                               | No example dashboards / dummy data                               |

Admin credentials are env-driven so the same scripts work for any
account name (`ADMIN_USERNAME`, `ADMIN_PASSWORD`,
`ADMIN_FIRSTNAME`, `ADMIN_LASTNAME`, `ADMIN_EMAIL`); the compose
file pins them to `admin` / `admin`.

## Volumes

| Volume          | Mount path           | What's persisted                                         | Wiped by `make nuke` |
|-----------------|----------------------|----------------------------------------------------------|----------------------|
| `superset-data` | `/app/superset_home` | SQLite metadata DB, dashboards, charts, saved queries, datasource configs | yes |

The `superset-init` container shares the same volume with the
long-running `superset`, so the admin user + database schema set up
by init survive into the webserver's lifetime.

## Healthcheck

`superset` only:

```yaml
test: curl --fail http://localhost:8088/health
interval: 30s
timeout:  10s
retries:  5
start_period: 90s   # gunicorn cold start + pip-install pinotdb on first run
```

`start_period: 90s` is generous because the bootstrap script does a
fresh pip install on every container start (see *Why this shape*
below) — first boot from a cold image cache adds ~15s. The init
container has no healthcheck (`restart: "no"` and the long-running
service depends on `service_completed_successfully`).

## Why this shape

- **Pip install at boot, not in a custom Dockerfile.** The
  `pinotdb` SQLAlchemy driver isn't in the upstream image. Baking
  a one-line Dockerfile would be tidier but adds a build step and
  a separate image to push. The Superset entrypoint already pip-
  installs from `/app/docker/requirements-local.txt` if it exists;
  bind-mounting our requirements file at that path is one fewer
  moving part. Trade-off: every container start re-runs `pip
  install` (~15s), since site-packages live in the image
  filesystem (not a volume) and reset between restarts.
- **Init/long-running split.** The official Superset compose
  example uses the same shape. The init container runs `superset
  db upgrade` + `superset fab create-admin` + `superset init`
  (roles + permissions) exactly once; the long-running service
  depends on it via `service_completed_successfully`. Keeps the
  admin user idempotent (`create-admin || true`) and avoids
  double-running the migration.
- **SQLite, not Postgres, for metadata.** Single-user class
  project; no concurrent dashboard editing. SQLite + a named
  volume = zero extra ceremony. To switch to multi-user, change
  `SQLALCHEMY_DATABASE_URI` in `superset_config.py` to
  `postgresql+psycopg2://airflow:airflow@postgres/superset` and
  create a `superset` database in the existing Airflow Postgres
  instance (not a separate cluster — see [`airflow.md`](airflow.md)).
- **`user: root` on both containers.** The init script does `pip
  install`, which needs to write to `/usr/local/lib/python*/site-
  packages`. Dropping privileges later isn't worth the complexity
  for a class project.
- **We bring our own `*-init.sh` / `*-bootstrap.sh`.** Superset
  4.1.1 dropped `/app/docker/{docker-init,docker-bootstrap}.sh`
  from the published image (those scripts only exist in the
  upstream source repo). Our minimal versions live in
  [`docker/superset/`](../../docker/superset/) and bind-mount
  into the container.
- **Database connections are dataset-config UI work, not container
  config.** After the stack is up, Superset → *Settings → Database
  Connections → +Database* with one of:

  | Target  | SQLAlchemy URI                                                                            |
  |---------|--------------------------------------------------------------------------------------------|
  | Pinot   | `pinot+http://pinot-broker:8099/query/sql?controller=http://pinot-controller:9000`          |
  | Presto  | `presto://presto-coordinator:8080/hive/default`                                             |

  Both use **in-network ports** (`8099`, `8080`) — Superset is in
  the same docker network as Pinot and Presto, so the host port
  remaps don't apply. Connections aren't in compose because Superset
  stores DB connections in its metadata DB, not env vars.

## Alternatives

Where Superset sits in the BI / dashboard landscape:

| System                  | Lineage                          | Pick instead when…                                                                                       |
|-------------------------|----------------------------------|----------------------------------------------------------------------------------------------------------|
| **Metabase**            | Closest open-source peer         | You want simpler setup and a friendlier UI for non-technical users. Less customizable, no equivalent of Superset's chart-builder flexibility. |
| **Looker**              | Paid, semantic-model-first       | You can pay for it and want a strongly governed semantic layer (LookML). Best-in-class for enterprise data governance. |
| **Tableau**             | Paid, vis-first                  | You want top-tier visualization primitives. Less SQL-friendly, expensive.                                |
| **Power BI**            | Paid, Microsoft-stack            | You're already in the Microsoft ecosystem. Tight integration with Excel and Azure.                       |
| **Grafana**             | Observability-first              | Time-series and metrics dashboards over Prometheus / InfluxDB / etc. Awkward for arbitrary SQL analytics. |
| **Redash**              | Direct peer (similar shape)      | Query-editor-first dashboards. Smaller community than Superset post-2020.                                |
| **Hex / Mode**          | Notebook-first analytics         | Paid, modern DX, blends notebook + dashboard. Strong for data-science-style exploration.                 |
| **Apache Zeppelin**     | Notebook-style                   | Analysts running Spark / Hive interactively, less for fixed dashboards.                                  |

Among open-source BI tools, **Superset and Metabase are the two
realistic picks**. Superset is more flexible and SQL-native;
Metabase has the friendlier UX. We picked Superset because the
`pinotdb` SQLAlchemy driver lets us connect straight to Pinot's
hybrid table without an intermediate warehouse.

## Common commands

CLI inside the container:

```sh
# List registered databases
docker compose exec superset superset db --help

# Re-run init (idempotent)
docker compose exec superset /app/superset-init.sh

# Drop a Python shell with full Superset context
docker compose exec superset superset shell
```

UIs:

- Superset: <http://localhost:8088> — `admin` / `admin`

## Caveats

- **`make nuke` deletes all dashboards.** Charts, datasets,
  saved-SQL — everything in the SQLite metadata DB on
  `superset-data`. Export dashboards to ZIP via the UI before any
  destructive cleanup if you've built up real artifacts.
- **Pip-install runs on every restart.** On a first cold start,
  this is ~15s of "why is the UI not responding yet?". On
  subsequent starts the wheels are usually cached locally so it's
  faster, but never instant. The healthcheck `start_period: 90s`
  accounts for this; if you tighten it you'll get false unhealthy
  reports.
- **Pinot connection caching.** Superset caches table metadata. If
  you alter a Pinot schema (add a column, change a type), refresh
  the dataset definition in Superset (*Datasets → ⋮ → Refresh*)
  or the new column won't show up in the chart designer.
- **No CSRF exemption for non-browser clients.** If you script
  Superset via REST, hit the `/security/csrf_token` endpoint
  first; the Flask default is to reject form posts without a
  token. Out of scope here, but worth knowing.
