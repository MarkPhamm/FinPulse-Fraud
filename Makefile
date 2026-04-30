SHELL := /bin/bash

# Detect compose command (compose v2 plugin vs legacy docker-compose)
COMPOSE ?= docker compose

.PHONY: help env up up-core up-bi up-stream down stop logs ps smoke smoke-hdfs smoke-kafka smoke-spark smoke-airflow smoke-pinot smoke-flink nuke

help:
	@echo "Targets:"
	@echo "  env           Create .env from .env.example with your UID"
	@echo "  up            Start the full stack (HDFS + Spark + Kafka + Airflow + Pinot + Superset + Flink)"
	@echo "  up-core       Start only HDFS + Spark + Kafka (skip Airflow / Pinot / Superset / Flink)"
	@echo "  up-bi         Start only Pinot + Superset (skip everything else)"
	@echo "  up-stream     Start only Kafka + Flink (skip everything else)"
	@echo "  down          Stop containers (keep volumes)"
	@echo "  stop          Same as down"
	@echo "  nuke          Stop and DELETE all volumes (HDFS, Kafka, Postgres, Pinot, Superset, Flink data)"
	@echo "  logs s=<svc>  Tail logs for one service, e.g. 'make logs s=namenode'"
	@echo "  ps            Show running services"
	@echo "  smoke         Run every smoke check (HDFS / Kafka / Spark / Airflow / Pinot / Flink)"
	@echo "  smoke-hdfs    HDFS put/get round-trip"
	@echo "  smoke-kafka   Produce + consume on a test topic"
	@echo "  smoke-spark   Submit a tiny PySpark job that reads HDFS"
	@echo "  smoke-airflow Trigger the smoke DAG and wait for success"
	@echo "  smoke-pinot   Check Pinot controller + broker /health, broker/server registered"
	@echo "  smoke-flink   Check Flink jobmanager /overview + at least 1 taskmanager registered"

env:
	@if [ -f .env ]; then echo ".env already exists"; else \
	  cp .env.example .env && echo "Wrote .env from .env.example"; fi

up:
	$(COMPOSE) up -d

up-core:
	$(COMPOSE) up -d namenode datanode-1 datanode-2 spark-master spark-worker-1 spark-worker-2 kafka kafdrop

up-bi:
	$(COMPOSE) up -d pinot-zookeeper pinot-controller pinot-broker pinot-server superset

up-stream:
	$(COMPOSE) up -d kafka kafdrop flink-jobmanager flink-taskmanager

down stop:
	$(COMPOSE) down

nuke:
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200 $(s)

smoke: smoke-hdfs smoke-kafka smoke-spark smoke-airflow smoke-pinot smoke-flink
	@echo ""
	@echo "============================================"
	@echo "  All smoke checks passed"
	@echo "  (HDFS / Kafka / Spark / Airflow / Pinot / Flink)"
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
