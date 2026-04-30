#!/usr/bin/env bash
# Smoke tests for the FinPulse infra stack.
# Run via:  make smoke   (or  make smoke-hdfs / smoke-kafka / smoke-spark / smoke-airflow)

set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}

c_green() { printf "\033[0;32m%s\033[0m\n" "$*"; }
c_red()   { printf "\033[0;31m%s\033[0m\n" "$*" 1>&2; }
c_blue()  { printf "\033[0;34m%s\033[0m\n" "$*"; }

step() { c_blue ""; c_blue "==> $*"; }
ok()   { c_green "    OK: $*"; }
die()  { c_red   "    FAIL: $*"; exit 1; }

# ----------------------------------------------------------------------------
hdfs_smoke() {
  step "HDFS: put / ls / cat / rm round-trip"

  # Use the namenode container itself as a thin HDFS client.
  $COMPOSE exec -T namenode bash -c '
    set -e
    echo "hello-finpulse" > /tmp/smoke.txt
    hdfs dfs -mkdir -p /smoke
    hdfs dfs -put -f /tmp/smoke.txt /smoke/smoke.txt
    hdfs dfs -ls /smoke
    out=$(hdfs dfs -cat /smoke/smoke.txt)
    test "$out" = "hello-finpulse" || { echo "got [$out]"; exit 1; }
    hdfs dfs -rm -r -skipTrash /smoke
  ' >/dev/null || die "HDFS round-trip failed"
  ok "HDFS round-trip works"
}

# ----------------------------------------------------------------------------
kafka_smoke() {
  step "Kafka: create topic + produce + consume"

  TOPIC="smoke-$(date +%s)"

  $COMPOSE exec -T kafka bash -c "
    set -e
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9094 \
        --create --if-not-exists --topic ${TOPIC} --partitions 1 --replication-factor 1 >/dev/null

    for i in 1 2 3 4 5; do echo \"msg-\$i\"; done | \
        /opt/kafka/bin/kafka-console-producer.sh \
        --bootstrap-server kafka:9094 --topic ${TOPIC} >/dev/null

    out=\$(/opt/kafka/bin/kafka-console-consumer.sh \
        --bootstrap-server kafka:9094 --topic ${TOPIC} \
        --from-beginning --max-messages 5 --timeout-ms 10000 2>/dev/null | wc -l | tr -d ' ')

    /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9094 \
        --delete --topic ${TOPIC} >/dev/null

    test \"\$out\" = \"5\" || { echo \"expected 5 messages, got \$out\"; exit 1; }
  " || die "Kafka produce/consume failed"
  ok "Kafka produce/consume works"
}

# ----------------------------------------------------------------------------
spark_smoke() {
  step "Spark: submit a job that reads from HDFS"

  # Seed an input file in HDFS first.
  $COMPOSE exec -T namenode bash -c '
    set -e
    printf "alpha bravo charlie\nalpha bravo\nalpha\n" > /tmp/words.txt
    hdfs dfs -mkdir -p /smoke
    hdfs dfs -put -f /tmp/words.txt /smoke/words.txt
  ' >/dev/null || die "could not seed HDFS input"

  $COMPOSE exec -T spark-master bash -c '
    /opt/spark/bin/spark-submit \
        --master spark://spark-master:7077 \
        --conf spark.hadoop.fs.defaultFS=hdfs://namenode:9000 \
        /opt/jobs/smoke_spark.py 2>&1
  ' | tee /tmp/finpulse-smoke-spark.log >/dev/null || die "spark-submit failed (see /tmp/finpulse-smoke-spark.log)"

  grep -q "SMOKE_OK total_words=6 distinct=3" /tmp/finpulse-smoke-spark.log \
      || die "spark job ran but produced wrong counts (see /tmp/finpulse-smoke-spark.log)"

  $COMPOSE exec -T namenode hdfs dfs -rm -r -skipTrash /smoke >/dev/null 2>&1 || true
  ok "Spark + HDFS integration works"
}

# ----------------------------------------------------------------------------
airflow_smoke() {
  step "Airflow: trigger smoke_dag and wait for success"

  $COMPOSE exec -T airflow-webserver bash -c '
    set -e
    airflow dags unpause smoke_dag >/dev/null
    airflow dags trigger smoke_dag >/dev/null
    # `airflow dags list-runs --output plain` columns:
    # 1=dag_id  2=run_id  3=state  4=execution_date  5=start_date  6=end_date
    for i in $(seq 1 30); do
      state=$(airflow dags list-runs -d smoke_dag --output plain 2>/dev/null | awk "NR==2 {print \$3}")
      if [ "$state" = "success" ]; then echo "success"; exit 0; fi
      if [ "$state" = "failed" ];  then echo "failed";  exit 1; fi
      sleep 2
    done
    echo "timeout"; exit 1
  ' || die "smoke_dag did not reach success state"
  ok "Airflow smoke DAG ran successfully"
}

# ----------------------------------------------------------------------------
pinot_smoke() {
  step "Pinot: controller + broker /health, cluster registration"

  # Hit /health on both. Curl from inside the controller container so we
  # don't depend on the host port mapping being up (the in-network names
  # are stable regardless of port remaps).
  $COMPOSE exec -T pinot-controller bash -c '
    set -e
    curl -fsS http://pinot-controller:9000/health | grep -q OK
    curl -fsS http://pinot-broker:8099/health   | grep -q OK
  ' >/dev/null || die "Pinot /health endpoints not OK"
  ok "Pinot controller + broker healthy"

  # Confirm the broker and server have actually registered with the cluster
  # (this catches misconfigured -zkAddress / clusterName / -*Host flags).
  out=$($COMPOSE exec -T pinot-controller \
      curl -fsS http://pinot-controller:9000/instances 2>/dev/null)
  echo "$out" | grep -q 'Broker_pinot-broker_8099' || die "broker not registered: $out"
  echo "$out" | grep -q 'Server_pinot-server_8098' || die "server not registered: $out"
  ok "Pinot broker + server registered with controller"
}

# ----------------------------------------------------------------------------
flink_smoke() {
  step "Flink: jobmanager /overview + taskmanager registration"

  # Hit /overview from inside the jobmanager container — same pattern as the
  # pinot smoke check, so we don't depend on host port mapping being up.
  out=$($COMPOSE exec -T flink-jobmanager curl -fsS http://localhost:8081/overview 2>/dev/null) \
      || die "jobmanager /overview not reachable"
  echo "$out" | grep -q '"flink-version"' || die "jobmanager /overview unexpected payload: $out"
  ok "Flink jobmanager healthy"

  # Confirm at least 1 taskmanager has registered with the jobmanager. Catches
  # misconfigured jobmanager.rpc.address / hostname mismatches.
  tm_count=$($COMPOSE exec -T flink-jobmanager curl -fsS http://localhost:8081/overview 2>/dev/null \
      | grep -oE '"taskmanagers":[0-9]+' | head -1 | cut -d: -f2)
  test "${tm_count:-0}" -ge 1 || die "no taskmanagers registered (got: ${tm_count:-0})"
  ok "Flink taskmanager registered (count=$tm_count)"
}

# ----------------------------------------------------------------------------
case "${1:-all}" in
  hdfs)    hdfs_smoke ;;
  kafka)   kafka_smoke ;;
  spark)   spark_smoke ;;
  airflow) airflow_smoke ;;
  pinot)   pinot_smoke ;;
  flink)   flink_smoke ;;
  all)     hdfs_smoke; kafka_smoke; spark_smoke ;;
  *)       echo "usage: $0 [hdfs|kafka|spark|airflow|pinot|flink|all]"; exit 2 ;;
esac
