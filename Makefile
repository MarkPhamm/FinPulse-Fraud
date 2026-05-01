SHELL := /bin/bash

# Detect compose command (compose v2 plugin vs legacy docker-compose)
COMPOSE ?= docker compose

# Hive Metastore Postgres JDBC driver (downloaded by `make hive-deps`).
HIVE_PG_JAR_VERSION := 42.7.2
HIVE_PG_JAR_PATH    := docker/hive-metastore/jars/postgresql-$(HIVE_PG_JAR_VERSION).jar

.PHONY: help env hive-deps up up-core up-bi up-stream up-dwh down stop logs ps smoke smoke-hdfs smoke-kafka smoke-spark smoke-airflow smoke-pinot smoke-flink smoke-presto nuke

help:
	@echo "Targets:"
	@echo "  env           Create .env from .env.example with your UID"
	@echo "  hive-deps     Download Postgres JDBC driver for the Hive Metastore (one-time, ~1.2 MB)"
	@echo "  up            Start the full stack (HDFS + Spark + Kafka + Airflow + Pinot + Superset + Flink + HMS + PrestoDB)"
	@echo "  up-core       Start only HDFS + Spark + Kafka (skip Airflow / Pinot / Superset / Flink / HMS / Presto)"
	@echo "  up-bi         Start the BI/serving layers (Pinot + Superset + HMS + Presto)"
	@echo "  up-stream     Start only Kafka + Flink (skip everything else)"
	@echo "  up-dwh        Start only the Hive Metastore stack (metastore-db + hive-metastore — no Presto)"
	@echo "  down          Stop containers (keep volumes)"
	@echo "  stop          Same as down"
	@echo "  nuke          Stop and DELETE all volumes (HDFS / Kafka / Airflow Postgres / Pinot / Superset / Flink / HMS Postgres data)"
	@echo "  logs s=<svc>  Tail logs for one service, e.g. 'make logs s=namenode'"
	@echo "  ps            Show running services"
	@echo "  smoke         Run every smoke check (HDFS / Kafka / Spark / Airflow / Pinot / Flink / Presto)"
	@echo "  smoke-hdfs    HDFS put/get round-trip"
	@echo "  smoke-kafka   Produce + consume on a test topic"
	@echo "  smoke-spark   Submit a tiny PySpark job that reads HDFS"
	@echo "  smoke-airflow Trigger the smoke DAG and wait for success"
	@echo "  smoke-pinot   Check Pinot controller + broker /health, broker/server registered"
	@echo "  smoke-flink   Check Flink jobmanager /overview + at least 1 taskmanager registered"
	@echo "  smoke-presto  Check Presto + Spark<->HMS<->Presto round-trip via smoke_presto.py"

env:
	@if [ -f .env ]; then echo ".env already exists"; else \
	  cp .env.example .env && echo "Wrote .env from .env.example"; fi

hive-deps:
	@mkdir -p $(dir $(HIVE_PG_JAR_PATH))
	@if [ -f $(HIVE_PG_JAR_PATH) ]; then \
	  echo "$(HIVE_PG_JAR_PATH) already present"; \
	else \
	  echo "Downloading postgresql-$(HIVE_PG_JAR_VERSION).jar -> $(HIVE_PG_JAR_PATH)"; \
	  curl -fsSL "https://jdbc.postgresql.org/download/postgresql-$(HIVE_PG_JAR_VERSION).jar" \
	    -o $(HIVE_PG_JAR_PATH); \
	fi

up:
	$(COMPOSE) up -d

up-core:
	$(COMPOSE) up -d namenode datanode-1 datanode-2 spark-master spark-worker-1 spark-worker-2 kafka kafdrop

up-bi: hive-deps
	$(COMPOSE) up -d pinot-zookeeper pinot-controller pinot-broker pinot-server superset \
	    metastore-db hive-metastore-init hive-metastore presto-coordinator

up-stream:
	$(COMPOSE) up -d kafka kafdrop flink-jobmanager flink-taskmanager

up-dwh: hive-deps
	$(COMPOSE) up -d metastore-db hive-metastore-init hive-metastore

down stop:
	$(COMPOSE) down

nuke:
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200 $(s)

smoke: smoke-hdfs smoke-kafka smoke-spark smoke-airflow smoke-pinot smoke-flink smoke-presto
	@echo ""
	@echo "============================================"
	@echo "  All smoke checks passed"
	@echo "  (HDFS / Kafka / Spark / Airflow / Pinot / Flink / Presto)"
	@echo "============================================"

smoke-hdfs:
	@bash scripts/smoke.sh hdfs

smoke-kafka:
	@bash scripts/smoke.sh kafka

smoke-spark:
	@bash scripts/smoke.sh spark

smoke-airflow:
	@bash scripts/smoke.sh airflow

smoke-pinot:
	@bash scripts/smoke.sh pinot

smoke-flink:
	@bash scripts/smoke.sh flink

smoke-presto:
	@bash scripts/smoke.sh presto
