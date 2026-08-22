-- 051: crm_event_raw (canon T13, A8) — the event spine's mailbox.
--
-- Everything that arrives, verbatim: store first, 200 fast, understand
-- later. Replay is the recovery mechanism that survives being wrong
-- about a schema. Payload is immutable; the envelope carries processing
-- state (processed_at, quarantine_reason, and the ADR 0020 customer_id
-- stamp, written by the resolve-and-journey processor).
--
-- DELIBERATE DEVIATION from the canon's monthly RANGE partitioning, for
-- phase 1 only: Postgres requires a partitioned table's unique
-- constraints to include the partition key, which would break the
-- load-bearing dedupe UNIQUE (merchant_id, source, external_id). The
-- dedupe wins. Partitioning (and partition-drop retention) arrives with
-- a later migration when pilot volume demands it — recorded in the
-- corpus canon trail, 23 Aug 2026.

CREATE TABLE crm_event_raw (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id       text NOT NULL,
    source            text NOT NULL,
    topic             text NOT NULL,
    schema_version    text NOT NULL DEFAULT '1',
    external_id       text NOT NULL,
    payload           jsonb NOT NULL,
    received_at       timestamptz NOT NULL DEFAULT now(),
    occurred_at       timestamptz,
    processed_at      timestamptz,
    quarantine_reason text,
    customer_id       uuid,
    UNIQUE (merchant_id, source, external_id)
);

-- The work queue IS this partial index (drained FOR UPDATE SKIP LOCKED).
CREATE INDEX crm_event_raw_pending_ix
    ON crm_event_raw (received_at)
    WHERE processed_at IS NULL;

-- The journey's commerce arm (ADR 0020): per-customer, time-ordered.
CREATE INDEX crm_event_raw_customer_ix
    ON crm_event_raw (merchant_id, customer_id, occurred_at)
    WHERE customer_id IS NOT NULL;

-- Topic-routed consumers read by (merchant, topic, time).
CREATE INDEX crm_event_raw_topic_ix
    ON crm_event_raw (merchant_id, topic, received_at);

-- The letter is immutable — by trigger, not by intention. Only the
-- processing envelope (processed_at, quarantine_reason, customer_id)
-- may ever change; a rewrite of the ingestion fields would corrupt
-- replay and the audit story while keeping the same event id.
CREATE OR REPLACE FUNCTION crm_event_raw_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.merchant_id     IS DISTINCT FROM OLD.merchant_id
       OR NEW.source          IS DISTINCT FROM OLD.source
       OR NEW.topic           IS DISTINCT FROM OLD.topic
       OR NEW.schema_version  IS DISTINCT FROM OLD.schema_version
       OR NEW.external_id     IS DISTINCT FROM OLD.external_id
       OR NEW.payload         IS DISTINCT FROM OLD.payload
       OR NEW.received_at     IS DISTINCT FROM OLD.received_at
       OR NEW.occurred_at     IS DISTINCT FROM OLD.occurred_at THEN
        RAISE EXCEPTION 'crm_event_raw ingestion fields are immutable — only processed_at, quarantine_reason and customer_id may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_event_raw_immutable_guard
    BEFORE UPDATE ON crm_event_raw
    FOR EACH ROW EXECUTE FUNCTION crm_event_raw_immutable();
