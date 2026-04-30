SHELL := /bin/bash

# Detect compose command (compose v2 plugin vs legacy docker-compose)
COMPOSE ?= docker compose

.PHONY: help env up up-core down stop logs ps smoke smoke-hdfs smoke-kafka smoke-spark smoke-airflow nuke

help:
	@echo "Targets:"
	@echo "  env           Create .env from .env.example with your UID"
	@echo "  up            Start the full stack (HDFS + Spark + Kafka + Airflow)"
	@echo "  up-core       Start only HDFS + Spark + Kafka (skip Airflow)"
	@echo "  down          Stop containers (keep volumes)"
	@echo "  stop          Same as down"
	@echo "  nuke          Stop and DELETE all volumes (HDFS, Kafka, Postgres data)"
	@echo "  logs s=<svc>  Tail logs for one service, e.g. 'make logs s=namenode'"
	@echo "  ps            Show running services"
	@echo "  smoke         Run end-to-end smoke test across HDFS, Kafka, Spark"
	@echo "  smoke-hdfs    HDFS put/get round-trip"
	@echo "  smoke-kafka   Produce + consume on a test topic"
	@echo "  smoke-spark   Submit a tiny PySpark job that reads HDFS"
	@echo "  smoke-airflow Trigger the smoke DAG and wait for success"

env:
	@if [ -f .env ]; then echo ".env already exists"; else \
	  cp .env.example .env && echo "Wrote .env from .env.example"; fi

up:
	$(COMPOSE) up -d

up-core:
	$(COMPOSE) up -d namenode datanode-1 datanode-2 spark-master spark-worker-1 spark-worker-2 kafka kafdrop

down stop:
	$(COMPOSE) down

nuke:
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200 $(s)

smoke: smoke-hdfs smoke-kafka smoke-spark
	@echo ""
	@echo "============================================"
	@echo "  All smoke checks passed."
	@echo "============================================"

smoke-hdfs:
	@bash scripts/smoke.sh hdfs

smoke-kafka:
	@bash scripts/smoke.sh kafka

smoke-spark:
	@bash scripts/smoke.sh spark

smoke-airflow:
	@bash scripts/smoke.sh airflow
