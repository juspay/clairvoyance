-- 062: crm_event_raw.attempts (canon T13; rollout phase 04, P2) — the
-- claim spends an attempt, so a poison row can be quarantined.
--
-- A row whose consumer raised deterministically (a bad live definition,
-- a DB error on one merchant) stayed processed_at IS NULL forever:
-- re-claimed every poll at the head of the queue (ORDER BY received_at),
-- re-running resolve()/assert_facts() each time, never quarantining.
-- Compare crm_workflow_enrollment.attempts (058): counted BY the claim,
-- so a crash mid-row counts against the row too. The event worker
-- quarantines the row once attempts reach CRM_EVENT_MAX_ATTEMPTS.
-- Quarantine, not delete — replay is the recovery mechanism: clear
-- processed_at/quarantine_reason by hand to re-drive the letter.
--
-- attempts is ENVELOPE, like processed_at, quarantine_reason and
-- customer_id. 051's immutability trigger denies changes to the
-- ingestion fields only, so it already admits this column; the function
-- is re-created here (CREATE OR REPLACE — the 060 amendment precedent,
-- never an edit to 051) only so its message names every mutable envelope
-- column truthfully. The trigger binding is unchanged.

ALTER TABLE crm_event_raw
    ADD COLUMN attempts smallint NOT NULL DEFAULT 0;

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
        RAISE EXCEPTION 'crm_event_raw ingestion fields are immutable — only processed_at, quarantine_reason, customer_id and attempts may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
