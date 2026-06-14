# Step 12 - Analysis notebook + business-impact report

This step delivers `notebooks/analysis.ipynb`: the seven business answers
from the brief, each read through the **serving layers** (Presto for
granular SQL, Pinot for pre-aggregated trends) and each ending in a dollar
or percentage tied to action.

This is the sister doc to
[`docs/plans/plan.md` Section Step 12](../plans/plan.md#step-12--analysis-notebook--business-impact-report)
- the plan defines the rubric, this doc is the runbook.

## What this step delivers

| Artifact | Purpose |
|---|---|
| `notebooks/analysis.ipynb` | 7 business questions + dual-engine trend + dollar-impact summary |

## What this step does NOT do

- **No path-based PySpark reads.** The notebook reads through Presto and
  Pinot - the same catalog Superset uses - never `spark.read.parquet`. HMS
  stays the single source of truth for what tables exist.
- **No model training.** Findings come from the rule-scored data
  (`analytics.scored`) and the dimensions; a trained classifier is an
  optional extension.
- **No Jupyter service in the stack.** Run the notebook from your own
  Jupyter/venv on the host (default ports) or any environment on the
  `finpulse` network (override the env vars).

## How the notebook connects

```python
from pyhive import presto                  # granular SQL over the lake
from pinotdb import connect as pinot_connect # pre-aggregated trends
```

Hosts/ports come from env vars with host-mapped defaults:

| Var | Default (host) | In-network |
|---|---|---|
| `PRESTO_HOST` / `PRESTO_PORT` | `localhost` / `8086` | `presto-coordinator` / `8080` |
| `PINOT_HOST` / `PINOT_PORT` | `localhost` / `8099` | `pinot-broker` / `8099` |

## The seven questions (and the verified findings)

| # | Question | Engine | Finding |
|---|---|---|---|
| 1 | Which signals predict fraud? | Presto | All rules ~2-3% precision vs the noisy label; `high_risk_merchant` edges `high_amount`. Rules are a triage funnel, not a verdict. |
| 2 | Recalibrate stale merchant risk | Presto | Actual fraud rate is ~flat (1.1-1.3%) for scores 1-7, ~2% for 8-10 - the stale score is a weak predictor. |
| 3 | Velocity attacks | Presto | Max 10-min per-card velocity is **2** - the 5-in-10-min rule never fires on this data; lower the threshold or treat as insurance. |
| 4 | Device / VPN risk | Presto | VPN and non-VPN have the same ~1.2-1.3% fraud rate - `is_vpn` is not discriminative alone. |
| 5 | Geographic risk | Presto | NG/RU/FR home countries run ~1.5% vs ~1.3% baseline - a minor weight, not a blocker. |
| 6 | False-positive analysis | Presto | ~97% of flagged transactions are false positives in both review and block queues - route to review, do not auto-block. |
| 7 | Customer behaviour anomaly | Presto | Confirmed-fraud txns average **2.5x** the card's baseline spend vs 1.0x for legit - the strongest single signal. |
| - | Dual-engine trend | Pinot | Risk-score distribution + dollar value per risk bucket, served pre-aggregated. |

## Pre-flight

```sh
# 1. Serving layers are populated.
docker compose exec presto-coordinator /opt/presto-cli --catalog hive \
    --schema analytics --execute 'SHOW TABLES'
curl -fsS -X POST http://localhost:8099/query/sql -d '{"sql":"SELECT count(*) FROM transactions_scored"}'

# 2. Clients installed where you run the notebook.
pip install "pyhive[presto]" pinotdb pandas requests
```

## Run it

Open `notebooks/analysis.ipynb` in Jupyter and run all cells, or execute
headless:

```sh
pip install "pyhive[presto]" pinotdb pandas requests jupyter
jupyter nbconvert --to notebook --execute notebooks/analysis.ipynb \
    --output analysis.executed.ipynb
```

## What "passes" looks like

The two highest-signal cells return:

```text
# Q7 - amount vs card baseline
 confirmed_fraud  avg_amount  ratio
           False      415.90   0.98
            True     1305.11   2.54

# Pinot - risk distribution
 risk_score      n
          0 429234
          1 527536
          2  37488
          3    205
```

And the business-impact summary prints caught vs missed confirmed fraud at
the average fraud amount (~$1,305), plus the false-positive review burden.

## Troubleshooting

- **`pandas only supports SQLAlchemy connectable` UserWarning.** Harmless -
  `pd.read_sql` works with the pyhive/pinotdb DBAPI connections; the warning
  is just pandas preferring SQLAlchemy.
- **Connection refused.** From the host use ports 8086 (Presto) / 8099
  (Pinot); from inside the network set the env vars to the service names.
- **Pinot table empty.** Replay the producer and confirm the Flink job is
  RUNNING (Step 7) so `transactions-scored` has fresh data.
- **Presto `Table not found`.** Run Step 9 (`register_hms_tables.py`) so
  the `analytics`/`curated` schemas exist.

## What's next

This is the last build step. The full pipeline now runs end to end: HDFS
lake -> Spark batch -> Kafka/Flink streaming -> Pinot + Presto serving ->
Superset + this notebook, orchestrated by Airflow. See
[`docs/plans/plan.md`](../plans/plan.md) for how each step maps back to the
rubric.
