# PrestoDB

PrestoDB is the **DWH serving layer for granular Parquet** that
[Spark](spark.md) writes into HDFS under `/curated/*` and
`/analytics/*`. It complements [Pinot](pinot.md), which serves the
*pre-aggregated streaming* layer. Two engines, two access patterns:

| Engine | Best for | Latency | Granularity |
|--------|----------|---------|-------------|
| Pinot  | Real-time dashboards, fixed schema, aggregated views from Kafka | sub-second | rolled-up segments |
| Presto | Ad-hoc SQL over the data lake, full row detail, joins across HDFS Parquet | seconds | raw granular rows |

Presto talks to the [Hive Metastore](#topology) (HMS) over Thrift to
discover what tables exist; HMS talks to a dedicated Postgres for
catalog storage; data bytes still live in HDFS. This is the
**lakehouse pattern** — three independent concerns: storage (HDFS),
metadata (HMS-on-Postgres), engines (Spark + Presto).

## Topology

| Service              | Image                       | Hostname             | Host → container ports | What other services talk to it                                  |
|----------------------|-----------------------------|----------------------|------------------------|------------------------------------------------------------------|
| `metastore-db`       | `postgres:16-alpine`        | `metastore-db`       | (no host ports)        | `hive-metastore-init`, `hive-metastore` (JDBC :5432)             |
| `hive-metastore-init`| `apache/hive:4.0.0`         | (default)            | (no host ports)        | One-shot — runs `schematool -initSchema` once, then exits        |
| `hive-metastore`     | `apache/hive:4.0.0`         | `hive-metastore`     | (no host ports)        | Spark (table register/lookup), Presto (catalog reads) on `:9083` |
| `presto-coordinator` | `prestodb/presto:0.292`     | `presto-coordinator` | `8086→8080`            | Browser (UI + REST), Superset, ad-hoc SQL clients                |

The HMS Thrift port `:9083` and the metastore Postgres `:5432` are
deliberately in-network only — only Spark and Presto need them, and
exposing them to the host buys nothing for a class project. Presto is
the only externally-reachable component (host port `8086`, container
port `8080` — moved off `8080` because Spark Master UI already owns
it on the host).

## Configuration source

Each component is configured by a different mechanism, so the table
covers all four:

### `metastore-db`

| Mechanism      | What it sets                                       |
|----------------|----------------------------------------------------|
| `environment:` | `POSTGRES_USER=hive`, `POSTGRES_PASSWORD=hive`, `POSTGRES_DB=metastore` |
| `volumes:`     | Named volume `metastore-db-data` → `/var/lib/postgresql/data`          |

### `hive-metastore-init` and `hive-metastore`

| Mechanism                    | What it sets                                                                                                                |
|------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| Bind mount `metastore-site.xml` → `/opt/hive/conf/metastore-site.xml` | JDBC URL, warehouse dir on HDFS, Thrift URI                                            |
| Bind mount `docker/hive-metastore/jars/` → `/opt/hive/jdbc`           | Postgres JDBC driver (downloaded by `make hive-deps`)                                  |
| Bind mount `docker/hadoop-client/` → `/opt/hadoop-conf`               | `core-site.xml` + `hdfs-site.xml` + `hive-site.xml` — same files Spark uses            |
| `entrypoint:` wrapper        | Copies the JDBC driver from `/opt/hive/jdbc/postgresql-*.jar` into `/opt/hive/lib/` before the image's stock entrypoint runs |
| `SERVICE_NAME=metastore`     | (long-running only) Tells the image's entrypoint to run the metastore daemon                                                 |
| `IS_RESUME=true`             | (long-running only) Skip `schematool -initSchema` since the init container already ran it                                    |

### `presto-coordinator`

| Mechanism                                                                                          | What it sets                                                       |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| Bind mount `docker/presto/etc/` → `/opt/presto-server/etc:ro`                                      | Replaces the image's default `etc/` directory wholesale            |
| `etc/config.properties`                                                                            | `coordinator=true`, `node-scheduler.include-coordinator=true` (single-node), HTTP port, query memory caps |
| `etc/jvm.config`                                                                                   | 3 GB heap, G1GC, `-Djdk.attach.allowAttachSelf=true` (required by spill-to-disk) |
| `etc/node.properties`                                                                              | `node.environment=finpulse`, node ID, data dir                     |
| `etc/log.properties`                                                                               | INFO log level                                                     |
| `etc/catalog/hive.properties`                                                                      | `connector.name=hive-hadoop2` (PrestoDB-specific), HMS Thrift URI, `hive.config.resources` pointing at the Spark-shared HDFS XML |
| Bind mount `docker/hadoop-client/` → `/etc/hadoop/conf:ro`                                         | HDFS client config — same XML files mounted into Spark master/workers, single source of truth for `fs.defaultFS` |

**The connector name is the single biggest gotcha.** PrestoDB's Hive
connector is `hive-hadoop2`. Trino renamed it to just `hive`. Putting
`hive` in PrestoDB's catalog file fails on startup with *"no factory
for connector 'hive'"*. See *Why this shape* for context on the
fork lineage.

## Volumes

| Volume               | Mount path                          | What's persisted                          | Wiped by `make nuke` |
|----------------------|-------------------------------------|-------------------------------------------|----------------------|
| `metastore-db-data`  | `/var/lib/postgresql/data`          | HMS catalog (table defs, partitions, statistics) | yes           |

Presto itself has **no persistent volumes** — query history and
spill state are ephemeral. HMS's only persistent state is the
Postgres catalog above. Parquet bytes live on HDFS (see
[`hdfs.md`](hdfs.md)) and survive HMS recreation; after `make nuke`
the `default.smoke_hms` table will need re-registering with
`saveAsTable` or `CREATE EXTERNAL TABLE`.

## Healthcheck

`hive-metastore` (Thrift TCP probe — there's no HTTP endpoint):

```yaml
test: bash -c 'echo > /dev/tcp/localhost/9083' || exit 1
interval: 10s
timeout:  5s
retries:  12
start_period: 30s
```

`presto-coordinator`:

```yaml
test: curl -fsS http://localhost:8080/v1/info | grep -q '"starting":false'
interval: 10s
timeout:  5s
retries:  18
start_period: 30s
```

Presto's `/v1/info` returns `{"starting": false, ...}` once all
catalogs have loaded and the cluster is ready to accept queries —
catches the case where the `hive` catalog file is malformed and the
cluster is up but unable to serve a `SHOW CATALOGS`. The longer
`start_period: 30s` matches Pinot's — JVMs take ~15-25s to come up.

`metastore-db` reuses the standard `pg_isready` healthcheck pattern.
The `hive-metastore-init` container is `restart: "no"` and has no
healthcheck; the long-running HMS depends on it via
`service_completed_successfully`.

## Why this shape

- **Two HMS containers, not one.** Init container runs
  `schematool -initSchema` *idempotently* (skips if a `schematool
  -info` query succeeds, so re-running `make up` doesn't re-init).
  Long-running container starts with `IS_RESUME=true` to bypass
  schema work entirely. Mirrors the [Airflow](airflow.md) and
  [Superset](superset.md) init/long-running split. Putting init
  inside the long-running entrypoint would either (a) re-run on
  every restart, polluting logs, or (b) require state-tracking on
  the host filesystem.
- **Dedicated Postgres for HMS.** Sharing Airflow's Postgres would
  couple lifecycles: `make nuke` on Airflow would silently wipe
  HMS's catalog. Different concern → different volume → different
  Postgres instance.
- **JDBC driver download via `make hive-deps`, not pre-baked.**
  The `apache/hive:4.0.0` image deliberately doesn't bundle a
  Postgres driver (Apache leaves DB choice to deployers). Same
  posture as the Spark Kafka connector (`--packages` at submit
  time): fetch deps on demand, don't bake images. The driver lives
  in `docker/hive-metastore/jars/` (gitignored). The
  `entrypoint:` wrapper copies it from a separate bind-mount path
  to `/opt/hive/lib/` at startup so the host directory mount
  doesn't shadow Hive's own lib dir.
- **Single-node Presto coordinator + embedded worker.** Production
  deployments split coordinators (plan/parse/dispatch) from workers
  (execute). For class scale we collapse them via
  `coordinator=true` + `node-scheduler.include-coordinator=true`.
  One JVM, one process, simpler reasoning. Adding workers later is
  one compose entry per worker — Presto auto-discovers them via
  the discovery service.
- **PrestoDB, not Trino.** The two are siblings: in 2020 the
  original Presto authors forked PrestoSQL → **Trino** after a
  Facebook trademark dispute, taking most of the active community
  with them. **PrestoDB** is the version still maintained by Meta
  / Facebook. Functionally they're 95% the same engine — same
  catalog model, same SQL dialect, same Hive connector
  architecture. We picked PrestoDB to match the historical
  reference architectures cited in
  [`docs/odsc/robinhood_infrastructure.md`](../odsc/robinhood_infrastructure.md);
  swap to Trino if you need newer connectors (Iceberg, Delta) or
  better arm64 support. The image launcher prints
  *"Support for the ARM architecture is experimental"* on Apple
  Silicon — advisory, not a failure.
- **Connector name `hive-hadoop2`.** See above. PrestoDB-specific.
- **`hive.config.resources` shares HDFS XML with Spark.** Both
  Spark and Presto bind-mount `docker/hadoop-client/` (Spark at
  `/opt/hadoop-conf`, Presto at `/etc/hadoop/conf`) and
  `hive.properties` references the latter. Single source of truth
  for `fs.defaultFS`, `dfs.replication`, etc. — change one file,
  both engines pick it up after a restart.
- **Port 8086 on the host.** Container port stays at `8080` (the
  PrestoDB default — Superset, `presto-cli`, and Presto's discovery
  URI all assume it). Host port `8086` was chosen because `8080` is
  already taken by the Spark Master UI. Same trick as Kafdrop
  (9001 ← 9000) and Pinot Controller (9100 ← 9000).
- **`hive.non-managed-table-writes-enabled=true`.** Lets Presto
  `INSERT INTO` external tables (tables registered against
  pre-existing HDFS Parquet via `CREATE EXTERNAL TABLE`). Default
  refuses these. Convenient for quick fixes from SQL Lab; Spark is
  still the primary writer.

## Alternatives

Where PrestoDB sits in the SQL-on-data-lake landscape:

| System              | Lineage                                | Pick instead when…                                                                                       |
|---------------------|----------------------------------------|----------------------------------------------------------------------------------------------------------|
| **Trino**           | The active fork of PrestoSQL (2020)    | You want first-class Iceberg / Delta / Hudi support, better arm64 ergonomics, or the broader community. Functionally near-identical to PrestoDB; mostly a community/maintenance choice. |
| **Apache Spark SQL**| Same engine that builds `/analytics/*` | You want one engine for ETL and ad-hoc — but Spark's interactive latency is much higher and JDBC support (Spark Thrift Server) is a second-class citizen. |
| **Apache Drill**    | Original schema-on-read SQL engine     | Heterogeneous unstructured sources (JSON, MongoDB) where you don't want HMS at all. Smaller community.   |
| **Apache Impala**   | Cloudera-stack peer (HMS + HDFS)       | You're already in CDH/CDP. Outside that ecosystem, Trino/PrestoDB have absorbed the mindshare.           |
| **DuckDB**          | Embedded analytical DB                 | Single-process notebook analytics over Parquet/CSV. Not a multi-user serving layer, no JDBC, no HMS.     |
| **ClickHouse**      | Open-source columnar DB                | You want to *load* the granular data into a real DWH (with duplication) for sub-second latency at scale. Different model from "SQL over the data lake." |
| **Snowflake / BigQuery** | Cloud warehouses                  | Batch-oriented historical analytics with a managed offering. Costs money, but no operational burden.     |
| **Dremio**          | Lakehouse query engine                 | You want a polished UX layer + reflections (materialized views) on top of S3/HDFS Parquet.               |

**Presto vs Trino is essentially a community/maintenance choice
within the same engine family.** Both read HDFS Parquet via the same
Hive connector pattern. ClickHouse is the most credible "load
granular data into a real DWH" alternative if Presto's sub-second
latency ever becomes a requirement; DuckDB is the right choice for
notebook analytics but not the multi-user serving role we need here.

## Common commands

UIs:

- Presto coordinator: <http://localhost:8086>

Presto CLI (lives at `/opt/presto-cli`, **not** on `$PATH`):

```sh
docker compose exec presto-coordinator /opt/presto-cli \
    --server localhost:8080 --execute 'SHOW CATALOGS'

docker compose exec presto-coordinator /opt/presto-cli \
    --catalog hive --schema default \
    --execute 'SELECT * FROM smoke_hms LIMIT 10'

# Interactive shell
docker compose exec -it presto-coordinator /opt/presto-cli \
    --catalog hive --schema default
```

REST API:

```sh
# Coordinator status — useful in scripts
curl -fsS http://localhost:8086/v1/info
# {"nodeVersion":{"version":"0.292-..."},"environment":"finpulse",...,"starting":false,...}

# Submit a query (returns a stateful URL to poll)
curl -X POST http://localhost:8086/v1/statement \
     -H "X-Presto-User: admin" \
     -H "X-Presto-Catalog: hive" \
     -H "X-Presto-Schema: default" \
     -d 'SELECT count(*) FROM smoke_hms'
```

HMS one-liners:

```sh
# Inspect HMS catalog directly via Postgres
docker compose exec metastore-db psql -U hive -d metastore -c '\dt'
docker compose exec metastore-db psql -U hive -d metastore \
    -c 'SELECT "TBL_NAME", "TBL_TYPE" FROM "TBLS";'

# Re-run schematool (idempotent — skips if schema exists)
docker compose exec hive-metastore-init bash -c \
    '/opt/hive/bin/schematool -dbType postgres -info'
```

Smoke check:

```sh
make smoke-presto   # /v1/info + hive catalog + Spark<->HMS<->Presto round-trip
```

Superset connection (UI: *Settings → Database Connections → +*):

| Field           | Value                                          |
|-----------------|------------------------------------------------|
| SQLAlchemy URI  | `presto://presto-coordinator:8080/hive/default` |

Note the **in-network port `8080`** — Superset is in the same
docker network as Presto, so it doesn't go through the host port
remap. Using `localhost:8086` in the URI breaks because Superset's
container can't resolve `localhost` to the Presto container.

## Caveats

- **Path-based Parquet writes are invisible to Presto.**
  `df.write.parquet("hdfs://...")` keeps working untouched — Spark
  writes the bytes, but no HMS entry exists. To make a path-based
  table queryable from Presto, register it with
  `CREATE EXTERNAL TABLE foo (...) WITH (external_location='hdfs://.../foo/', format='PARQUET')`.
  See [`docs/infrastructure/spark.md`](spark.md) — *Hive Metastore
  integration* for the alternative `df.write.saveAsTable()` flow
  that registers automatically.
- **`make hive-deps` is required before first `make up`.** Without
  it, `hive-metastore-init` fails fast with *"postgres JDBC driver
  missing in `./docker/hive-metastore/jars/`. Run 'make hive-deps'
  on the host."* The download is ~1.2 MB and one-shot.
- **arm64 is "experimental" per the launcher banner.** Functionally
  fine for class-scale workloads in 2026; performance may lag amd64
  by single-digit percent. Trino has stronger arm64 commitment if
  this ever matters.
- **Schema drift between HMS and the actual Parquet files is not
  validated at query time.** If Spark rewrites a Parquet column
  from `INT` to `BIGINT` without updating the HMS table definition,
  Presto reads the file with the old type and may surface odd
  values or errors. Stick with `df.write.saveAsTable()` /
  `df.write.insertInto()` rather than mixing path-based writes
  with HMS-registered tables.
- **No auth.** Presto is HTTP-anonymous in this dev cluster (same
  posture as Pinot). Don't expose host port 8086 outside the
  laptop.
- **Memory budget.** HMS + metastore-db + Presto add ~3 GB
  resident on top of the existing ~12 GB stack. On a 16 GB Docker
  allocation this is tight when Pinot is also up — see the README
  *Prerequisites* (≥18 GB recommended for the full stack), or use
  `make up-dwh` / `make up-bi` for partial bring-up.
