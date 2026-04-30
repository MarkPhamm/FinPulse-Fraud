# Real-Time Event-Time Consistent Analytics Pipelines using Kafka, Flink, and Apache Pinot

**Speaker:** Deep Patel — Senior Data Engineer at Robinhood
**Conference:** ODSC AI East 2026
**Session:** Day 1 (April 28), 12:20 PM – 12:50 PM, 30 min, Virtual
**Track:** Data Engineering · Intermediate–Advanced · Talk

## Abstract

Real-time decision systems increasingly depend on complex data pipelines
that must deliver fresh, reliable insights within seconds. Whether powering
trading platforms, fraud detection, operational analytics, or user-facing
dashboards, organizations now expect analytical systems to handle massive
event streams with both low latency and high correctness. Yet, while
technologies such as Apache Kafka, Apache Flink, and Apache Pinot have
become foundational components of modern data architectures, the industry
still lacks a unified model for ensuring consistency and correctness across
the full end-to-end pipeline.

This session introduces a comprehensive framework for achieving event-time-
consistent, exactly-once real-time analytics across Kafka → Flink → Pinot
pipelines. Drawing from real production challenges encountered at scale —
including late-arriving events, reprocessing and replay, ingestion
backpressure, and multi-tenant OLAP workloads — the presentation identifies
the fundamental sources of metric drift and inconsistency within current
streaming + OLAP architectures.

Attendees will learn how to apply event-time watermarks beyond the streaming
layer and into the OLAP ingestion process, enabling predictable completeness
windows and preventing partial or inconsistent aggregates from surfacing in
dashboards. The session also presents an ingestion protocol that combines
deterministic segment assignment, idempotent upserts, and Flink checkpoint
alignment to ensure replay-safe correctness. The result is a system that
reduces aggregate divergence, smooths ingestion latency, and improves
reliability under real-world workloads.

Participants will walk away with actionable architectural patterns,
correctness models, and practical implementation strategies that can be
deployed in any organization building real-time analytical systems. This
talk bridges the gap between streaming theory and OLAP practice, offering
a blueprint for the next generation of trustworthy, high-fidelity real-time
analytics.

---

## 1. Lesson of History — Why batch analytics wasn't enough at Robinhood

Three pain points that forced the move away from batch-only:

**Dashboard Lag**

- LookMLs were a PITA
- Presto clusters were slow and expensive
- No standard cube materialization
- Needed a low-latency, high-QPS OLAP database
- Migrate to Superset

**Real-time Gap**

- Streaming used only for fraud and feature engineering
- Analytics still ran on batch pipelines

**Blind Spot**

- No support for "datasets"
- Volume and order-flow analysis ran 30+ minutes behind
- Too late for product launches and live experimentation

> **Takeaway:** Real-time analytics needed to be a first-class platform,
> not a fraud-team side project.

---

## 2. Session Goal

> Build a real-time analytics platform enabling **low-latency ingestion**,
> **sub-second queries**, and **event time-consistency** over streaming
> data with reconciliation.

Three pillars:

- Low-latency ingestion
- Fast queries
- Eventually correct insights

---

## 3. Design Goals & Constraints

Framed as a "Blackberry vs iPhone" analogy — current state of real-time
data analysis is "Blackberry; iPhone coming soon."

Required properties:

- Near real-time availability (< minutes)
- Eventual correctness
- Support for both fresh and historical analysis
- Reconciliation
- Fast, interactive querying at scale
- Horizontal scalability
- Scalable serving for operational dashboards and investigations

---

## 4. High-Level Architecture

```text
                   ┌──────────────────┐
   Datalake ─────► │      Spark       │ ── Offline ingestion ─┐
                   └──────────────────┘                       │
                                                              ▼
   Fact Event Kafka Stream ────────► ┌──────────┐  Real-time┌────────┐
                                     │ Flink App│ ─ kafka  ►│ Pinot  │ ◄── Downstream consumer Retriever API 
   Dimension Event Kafka Stream ───► │  (enrich)│           └────────┘
                                     └──────────┘
```

Components:

- **Kafka** — durable event streams
- **Apache Flink** — stateful stream processing
- **Apache Pinot** — real-time analytical serving
- **Batch layer (Spark)** — for reconciliation
- **Front-end layer** via API

---

## 5. Why Flink

Three core properties:

1. **Event-time native** — bucketing by when it happened, not when we saw it
2. **Stateful at scale** — RocksDB state survives failures, no recovery code
3. **Exactly-once** — two-phase commit with Kafka, no duplicate transactions

Comparison matrix (✓ = yes, ✗ = no, ~ = partial):

| Capability        | Kafka Streams | Spark | Flink |
|-------------------|:-------------:|:-----:|:-----:|
| Event-time        |       ~       |   ~   |   ✓   |
| True streaming    |       ✓       |   ✗   |   ✓   |
| State mgmt        |       ~       |   ✓   |   ✓   |
| Exactly-once      |       ✓       |   ✓   |   ✓   |
| ms latency        |       ✓       |   ✗   |   ✓   |

> Only Flink hits all five — simultaneously.

Note: Already running at Robinhood for fraud & feature engineering.

---

## 6. Event Time vs Processing Time

> Incorrect time bucketing in financial analytics isn't a bug — it's a
> compliance risk.

Three regimes:

- **Event time ✓** — Trade in 9:31 window. Correct for audits.
- **Processing time ✗** — Trade in 9:35 window. Compliance risk.
- **Very late data** — Side output → Spark corrects offline.

Mechanism: a watermark of `T − 5min` defines a "within tolerance" boundary.
Events past the watermark are routed to a side output for offline correction.

---

## 7. What is Apache Pinot?

> The fastest real-time distributed OLAP database — designed for low-latency
> queries at extreme throughput.

Three defining traits:

- **Columnar + pluggable indexing** — 12 index types, configurable per
  column and per segment. Pushes the boundaries of query performance.
- **Real-time ingestion (not micro-batching)** — millions of events/sec
  ingested and queryable immediately. No waiting on batch windows.
- **Distributed, scalable, fault tolerant** — spans thousands of nodes
  acting as one system. Replicated segments, no single point of failure.

Three qualities highlighted: Concurrency · Data freshness · Query latency.

---

## 8. Apache Pinot's Scale (production references)

| Company   | Scale                                                      |
|-----------|------------------------------------------------------------|
| LinkedIn  | 1M+ events/sec · 200K+ queries/sec · ms query latency       |
| Stripe    | 1PB+ data size · 20K+ queries/sec · < 25ms query latency    |
| Uber      | 200TB+ data size · 30K+ queries/sec · < 100ms query latency |

---

## 9. Segments: the storage atom

> Pinot data is partitioned into **immutable, columnar segments**
> (~100MB–1GB). Each segment is independently indexed, replicated, and
> prunable.

Lifecycle:

- **Consuming (in-memory)** — server tails Kafka, builds an in-memory
  mutable segment with row-store indexes.
- **Completed (sealed on disk)** — threshold hit → flushed to columnar,
  fully indexed, immutable segment on disk.
- **Deep store (S3 / GCS)** — persisted to deep store; servers can
  rehydrate on restart or rebalance.

Why segments matter:

- **Unit of parallelism** — queries fan out across segments
- **Unit of pruning** — broker skips segments by metadata (time, partition)
- **Unit of tiering** — hot in memory, warm on disk, cold in deep store
- **Unit of replication & rebalancing** — cluster ops are segment moves

---

## 10. Real-time Upserts: per-key state, sub-second

> Native data mutability — unlike append-only OLAPs that need
> application-layer compensation. Reconciled in seconds during ingestion.

**Full upsert** — new record completely replaces the prior version for
that key. Good for slowly-changing dimensions, order/customer state, CDC
from OLTP.

**Partial upsert** — new record updates only specified columns; others
retain prior values. Good for multi-source enrichment, late-arriving
fields, dimension stitching.

**Massive production footprint:**

- 10s of millions of msg/s
- 10s of 1000s of QPS
- Billions of primary keys

**Requirements:**

- Partition by PK in Kafka and Pinot (same hash)
- Comparison column with monotonic ordering
- Costs memory: PK → location map kept in heap

---

## 11. Flexible Indexing — pick the right tool per column

> Pluggable, per-column, per-segment — pick the right index for each
> access pattern.

| Index         | Use case                                                            |
|---------------|---------------------------------------------------------------------|
| Vector        | Embedding similarity, RAG, semantic search                          |
| Range         | BETWEEN / numeric range predicates                                  |
| Timestamp     | Time-window & series predicates                                     |
| Inverted      | Equality filters on high-cardinality cols                           |
| JSON          | Query nested payloads inline                                        |
| Bloom filter  | Point lookups, segment pruning                                      |
| Sorted        | One per table, massive speedup on that col                          |
| Text (Lucene) | Full-text & substring search                                        |
| Forward       | Default columnar storage                                            |
| Star-tree     | Pre-aggregated cubes for predictable p99                            |
| Geospatial(H3)| Distance & polygon containment                                      |
| + pluggable   | Custom index types — extend the engine                              |

> Single platform across user-facing analytics, personalization,
> time-series, anomaly detection, and ad-hoc.

---

## 12. Multi-Stage Engine: joins, finally

> Production-grade multi-stage engine — supports all join types,
> sub-queries, window functions, and set operations across real-time and
> historical tables.

Three join strategies illustrated:

- **Broadcast Join** — left table data, post filter, vs right table
  data, post filter
- **Shuffle Hash Distributed Join** — both sides shuffled
- **Lookup Join** — right table data, pre-loaded

Legend:

- L = Left Table Data, Post Filter
- R = Right Table Data, Post Filter
- (open circle) = Right Table Data, Pre-Loaded

---

## 13. Hybrid Tables: fresh + correct, unified

> A **hybrid table** is one logical table backed by two physical tables —
> a real-time table fed by Kafka and an offline table built by batch jobs.
> The broker unions them at query time.

**Real-time path** — Kafka → consuming segments → sealed segments.
Seconds-fresh, but at the mercy of producer drift, late arrivals, and
schema edge cases.

**Offline path · nightly Spark** — Warehouse → Spark → segments → Pinot.
Reconciles against the source of truth each night — deterministic,
replayable, idempotent.

Properties:

- Same schema, same query surface
- Bootstrapping: bootstrapped data via offline ingestion

**How the broker resolves a query:** picks offline segments wherever they
exist and fills the gap up to *now* from the real-time table. Yesterday
and earlier comes from the reconciled copy; today comes from Kafka.

```text
Query routing
  WHERE date <  'Aug 2'   →  served from OFFLINE
  WHERE date >= 'Aug 2'   →  served from REALTIME

Segment timeline (cutover at Aug 2)

  Offline    | Jul 31 1 | Jul 31 2 | Aug 1 1  | Aug 1 2  | Aug 2 1  | Aug 2 2  |
  Realtime                                    | Aug 1 2  | Aug 2 1  | Aug 2 2  | Aug 3 1  | Aug 3 2  |  ──► time
                                                         ↑
                                                  cutover boundary
                                       (broker uses offline before, realtime after)
```

---

## 14. How We Made Catbox a Reusable Platform

"Catbox-Date — 4 steps for any team to onboard":

1. **Find Your Events** — locate the Kafka topic for your use case, or
   request one from the platform team
2. **Declare Sources & Sinks** — drop a config YAML that points at your
   input topic and target output
3. **Write Your SQL** — express your business logic as a Flink SQL query.
   No Java, no cluster setup
4. **Ship It** — the platform deploys, manages infra, and serves results
   via Pinot automatically

> Any team can launch a real-time analytics pipeline in under a day —
> no infra knowledge required.

---

## 15. Where Joins Really Belong

> Stream-stream joins in Flink **look free** — until you see the
> state-size bill, the watermark coupling, and the silent gaps from late
> data.

**Why joining in Flink hurts:**

- State doubles — both sides held until the window closes
- Watermark = min(streams) — the slowest source paces everyone
- Late events leave silent gaps in joined output
- Multi-way joins compound state combinatorially
- Cross-stream schema drift becomes a debugging nightmare

**Where to put joins instead:**

- Denormalize at the producer — one event carries its context
- Pinot lookup joins for small, cacheable dimensions
- Pinot multi-stage joins for fact ↔ fact at query time
- CDC slow-changing dimensions into Pinot upsert tables
- Reserve Flink for what only it does: windows + exactly-once

> **Rule of thumb:** if the join can happen at read time in Pinot, it
> usually should.

---

## 16. Dashboard experience changed dramatically

- **Looker → Superset** migration was game-changing
- **Executive** dashboards first
- "Click on a dimension and see your mind blown"

Slack-branded executive dashboard sample showed: 11 (+6.7% WoW),
55 (members in Slack), 139 members on Slack, total dashboard count 2.09k.

---

## 17. What changed — the numbers we cared about

**Dashboard P95**: 58s → **800ms** (30× faster)

**Data freshness**: T+24h → **≈ 5s** (near-real-time)

**Concurrent users**: ≈ 50 → **500+** (10× concurrency)

Other production-scale numbers:

- 150+ analytics tables on Pinot
- 250k/s events ingested at peak
- 8k+ QPS Superset traffic at peak
- 99.95% query SLA

---

## How this maps to FinPulse-Fraud

Notes for cross-referencing this talk with the local stack
(`docker-compose.yml`):

- The talk's **Kafka → Flink → Pinot** pipeline corresponds directly to
  this repo's intended `src/producer/` (Kafka producers) →
  `src/consumer/` (Flink-style stream consumers) → `pinot-*` services.
- The **hybrid-table** pattern (Kafka real-time table + nightly
  Spark-built offline table, broker unions at query time) is the natural
  follow-up to wiring the existing `jobs/` Spark batch path into Pinot
  alongside the Kafka real-time table.
- "Where joins really belong" argues for denormalization at the producer
  and Pinot lookup joins instead of stream-stream joins — relevant when
  designing how `transactions`, `device-fingerprints`, `customer-profiles`,
  `merchant-directory`, and `fraud-reports` are joined for fraud scoring.
- The Catbox 4-step pattern (find topic → declare config → write SQL →
  ship) is a template for how to expose this stack to non-infra users
  via Airflow + a YAML config layer.
