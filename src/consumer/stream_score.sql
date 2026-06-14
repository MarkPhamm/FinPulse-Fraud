-- FinPulse real-time fraud scoring (Step 7), Flink SQL.
--
-- Reads Kafka topic `transactions` with an event-time watermark, computes
-- a keyed 10-minute velocity count, enriches each event with the customer
-- baseline from the `customer-features` topic (upsert-kafka versioned
-- table), applies the streaming rules, and writes every scored event to
-- `transactions-scored` plus high-risk events to `fraud-alerts`. Both sinks
-- are upsert-kafka keyed by txn_id, so a checkpoint replay re-emits the
-- same key and the latest value wins (idempotent, exactly-once-effective).
--
-- Submit:
--   docker compose exec -T flink-jobmanager \
--     /opt/flink/bin/sql-client.sh -f /opt/flink/usrlib/stream_score.sql

SET 'pipeline.name' = 'finpulse-stream-score';
-- Bound the join/window state so an unbounded source cannot grow forever.
SET 'table.exec.state.ttl' = '30 min';

-- ---------------------------------------------------------------------------
-- Source: the transactions fact stream. Values are JSON strings (the Step 3
-- producer json.dumps'd csv rows), so amount / is_international arrive as
-- strings and the event timestamp is "yyyy-MM-dd HH:mm:ss".
-- ---------------------------------------------------------------------------
CREATE TABLE transactions_src (
    txn_id            STRING,
    `timestamp`       STRING,
    card_id           STRING,
    merchant_id       STRING,
    amount            STRING,
    currency          STRING,
    merchant_category STRING,
    country           STRING,
    channel           STRING,
    is_international   STRING,
    event_time        AS TO_TIMESTAMP(`timestamp`),
    WATERMARK FOR event_time AS event_time - INTERVAL '2' MINUTE
) WITH (
    'connector'                = 'kafka',
    'topic'                    = 'transactions',
    'properties.bootstrap.servers' = 'kafka:9094',
    'properties.group.id'      = 'flink-stream-score',
    'scan.startup.mode'        = 'earliest-offset',
    'format'                   = 'json',
    'json.ignore-parse-errors' = 'true'
);

-- ---------------------------------------------------------------------------
-- Dimension: customer baseline, published to Kafka by
-- jobs/features/publish_customer_features.py. upsert-kafka materializes the
-- latest value per card_id (the key), so this behaves like a versioned
-- lookup table without a separate datastore.
-- ---------------------------------------------------------------------------
CREATE TABLE customer_features_src (
    card_id              STRING,
    avg_monthly_spend    DOUBLE,
    home_country         STRING,
    seen_countries       ARRAY<STRING>,
    unique_country_count INT,
    PRIMARY KEY (card_id) NOT ENFORCED
) WITH (
    'connector'             = 'upsert-kafka',
    'topic'                 = 'customer-features',
    'properties.bootstrap.servers' = 'kafka:9094',
    'key.format'            = 'raw',
    'value.format'          = 'json',
    'value.fields-include'  = 'EXCEPT_KEY'
);

-- ---------------------------------------------------------------------------
-- Dimension: merchant risk, published to Kafka by
-- jobs/features/publish_merchant_directory.py. Same upsert-kafka versioned
-- lookup shape as the customer baseline.
-- ---------------------------------------------------------------------------
CREATE TABLE merchant_features_src (
    merchant_id         STRING,
    merchant_risk_score INT,
    PRIMARY KEY (merchant_id) NOT ENFORCED
) WITH (
    'connector'             = 'upsert-kafka',
    'topic'                 = 'merchant-directory',
    'properties.bootstrap.servers' = 'kafka:9094',
    'key.format'            = 'raw',
    'value.format'          = 'json',
    'value.fields-include'  = 'EXCEPT_KEY'
);

-- ---------------------------------------------------------------------------
-- Sink: every scored event (feeds Pinot's real-time table in Step 8).
-- ---------------------------------------------------------------------------
CREATE TABLE transactions_scored_sink (
    txn_id              STRING,
    card_id             STRING,
    event_time          TIMESTAMP(3),
    merchant_id         STRING,
    amount              DOUBLE,
    country             STRING,
    channel             STRING,
    is_international     BOOLEAN,
    velocity_count          BIGINT,
    merchant_risk_score     INT,
    rule_high_amount        BOOLEAN,
    rule_velocity           BOOLEAN,
    rule_intl_mismatch      BOOLEAN,
    rule_high_risk_merchant BOOLEAN,
    risk_score              INT,
    predicted_fraud         BOOLEAN,
    recommended_action      STRING,
    PRIMARY KEY (txn_id) NOT ENFORCED
) WITH (
    'connector'             = 'upsert-kafka',
    'topic'                 = 'transactions-scored',
    'properties.bootstrap.servers' = 'kafka:9094',
    'key.format'            = 'raw',
    'value.format'          = 'json'
);

-- ---------------------------------------------------------------------------
-- Sink: high-risk events only (feeds the Superset alert ticker).
-- ---------------------------------------------------------------------------
CREATE TABLE fraud_alerts_sink (
    txn_id              STRING,
    card_id             STRING,
    event_time          TIMESTAMP(3),
    amount              DOUBLE,
    risk_score          INT,
    triggered_rules     STRING,
    recommended_action  STRING,
    PRIMARY KEY (txn_id) NOT ENFORCED
) WITH (
    'connector'             = 'upsert-kafka',
    'topic'                 = 'fraud-alerts',
    'properties.bootstrap.servers' = 'kafka:9094',
    'key.format'            = 'raw',
    'value.format'          = 'json'
);

-- ---------------------------------------------------------------------------
-- Velocity: count of this card's transactions in the trailing 10 minutes.
-- OVER windows preserve the event-time attribute and emit append-only rows.
-- ---------------------------------------------------------------------------
CREATE TEMPORARY VIEW txn_velocity AS
SELECT
    txn_id, card_id, merchant_id, amount, country, channel,
    is_international, event_time,
    COUNT(*) OVER (
        PARTITION BY card_id
        ORDER BY event_time
        RANGE BETWEEN INTERVAL '10' MINUTE PRECEDING AND CURRENT ROW
    ) AS velocity_count
FROM transactions_src;

-- ---------------------------------------------------------------------------
-- Score: enrich with the customer baseline and apply the streaming rules.
-- Only the three rules computable from the transaction stream + customer
-- baseline are scored here; merchant/device rules are reconciled offline
-- (Step 6), matching the real-time-partial / offline-complete pattern.
-- ---------------------------------------------------------------------------
-- COALESCE every rule to FALSE: a transaction whose card baseline has not
-- been loaded into the join state yet (or has no baseline at all) must score
-- a definite 0, never SQL NULL — otherwise null arithmetic nulls risk_score.
CREATE TEMPORARY VIEW scored AS
SELECT
    v.txn_id,
    v.card_id,
    v.event_time,
    v.merchant_id,
    CAST(v.amount AS DOUBLE)                              AS amount,
    v.country,
    v.channel,
    (v.is_international = 'true')                         AS is_international,
    v.velocity_count,
    COALESCE(
        c.avg_monthly_spend IS NOT NULL
        AND CAST(v.amount AS DOUBLE) > 3 * c.avg_monthly_spend / 30,
        FALSE)                                           AS rule_high_amount,
    (v.velocity_count >= 5)                              AS rule_velocity,
    COALESCE(
        (v.is_international = 'true')
        AND c.unique_country_count = 1
        AND ARRAY_CONTAINS(c.seen_countries, c.home_country),
        FALSE)                                           AS rule_intl_mismatch,
    m.merchant_risk_score                                AS merchant_risk_score,
    COALESCE(m.merchant_risk_score >= 8, FALSE)          AS rule_high_risk_merchant
FROM txn_velocity v
LEFT JOIN customer_features_src c ON v.card_id = c.card_id
LEFT JOIN merchant_features_src m ON v.merchant_id = m.merchant_id;

CREATE TEMPORARY VIEW scored_final AS
SELECT
    txn_id, card_id, event_time, merchant_id, amount, country, channel,
    is_international, velocity_count, merchant_risk_score,
    rule_high_amount, rule_velocity, rule_intl_mismatch, rule_high_risk_merchant,
    (CAST(rule_high_amount AS INT)
        + CAST(rule_velocity AS INT)
        + CAST(rule_intl_mismatch AS INT)
        + CAST(rule_high_risk_merchant AS INT)) AS risk_score
FROM scored;

-- Run both sinks inside one job so they share a checkpoint.
EXECUTE STATEMENT SET
BEGIN
    INSERT INTO transactions_scored_sink
    SELECT
        txn_id, card_id, event_time, merchant_id, amount, country, channel,
        is_international, velocity_count, merchant_risk_score,
        rule_high_amount, rule_velocity, rule_intl_mismatch, rule_high_risk_merchant,
        risk_score,
        (risk_score >= 2) AS predicted_fraud,
        CASE WHEN risk_score >= 3 THEN 'block'
             WHEN risk_score >= 2 THEN 'review'
             ELSE 'approve' END AS recommended_action
    FROM scored_final;

    INSERT INTO fraud_alerts_sink
    SELECT
        txn_id, card_id, event_time, amount, risk_score,
        CONCAT_WS(',',
            CASE WHEN rule_high_amount        THEN 'high_amount'        ELSE NULL END,
            CASE WHEN rule_velocity           THEN 'velocity'           ELSE NULL END,
            CASE WHEN rule_intl_mismatch      THEN 'intl_mismatch'      ELSE NULL END,
            CASE WHEN rule_high_risk_merchant THEN 'high_risk_merchant' ELSE NULL END
        ) AS triggered_rules,
        CASE WHEN risk_score >= 3 THEN 'block' ELSE 'review' END AS recommended_action
    FROM scored_final
    WHERE risk_score >= 2;
END;
