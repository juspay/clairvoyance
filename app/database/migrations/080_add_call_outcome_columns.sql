-- Migration: call outcome columns (connection / agent / eval layers)
-- Description: lead_call_tracker.outcome is one free-text, last-writer-wins
-- column that mixes four owners: the carrier (NO_ANSWER), the dispatcher
-- (PRECHECK_FAILED, BLACKLISTED, ...), the pipeline's own fallbacks (BUSY on
-- idle timeout / hangup, UNKNOWN, EARLY_HANGUP) and the agent (CONFIRM,
-- CANCEL, ...). These columns split it into three layers, each with ONE
-- writer:
--
--   connection  connection_status, connection_reason, provider_status,
--               hangup_cause, end_reason
--               — the system (dispatcher, inbound policy, carrier callback,
--               pipeline). Never an LLM.
--   agent       agent_outcome, outcome_source
--               — the agent: LLM functions, IVR options, observers.
--   eval        eval_outcome, eval_status, eval_result_id
--               — the post-call evaluation (PR #1207 fills these; they stay
--               NULL until it lands).
--
-- Phase 1 only WRITES these, beside the legacy column, which stays
-- byte-for-byte as today; nothing reads them yet. Writes are gated by the
-- CALL_OUTCOME_WRITES_ENABLED dynamic flag (default off), so the code may ship
-- before this migration runs.
--
-- Every column is nullable with no default: ADD COLUMN is metadata-only, no
-- table rewrite. Vocabulary lives in code (app/schemas/breeze_buddy/
-- outcomes.py), never in CHECKs; format CHECKs arrive with the legacy
-- column's removal, once every writer is proven.
--
-- chat_session gets the agent and eval layers only: its ended_reason already
-- records how a session ended, and a chat has no carrier leg.
--
-- call_execution_config.retry_policy is the per-template switch for the
-- connection-driven retry rules (phase 3). DEFAULT 'LEGACY' keeps every
-- existing and new config on today's BUSY / NO_ANSWER retry rule; Postgres
-- stores the default without rewriting the table.
--
-- Plan, decisions and phases: docs/CALL_OUTCOMES.md.

ALTER TABLE lead_call_tracker
    ADD COLUMN connection_status varchar(30),
    ADD COLUMN connection_reason varchar(50),
    ADD COLUMN provider_status   varchar(50),
    ADD COLUMN hangup_cause      varchar(100),
    ADD COLUMN end_reason        varchar(50),
    ADD COLUMN agent_outcome     varchar(50),
    ADD COLUMN outcome_source    varchar(20),
    ADD COLUMN eval_outcome      varchar(50),
    ADD COLUMN eval_status       varchar(20),
    ADD COLUMN eval_result_id    uuid
        REFERENCES evaluation_result(id) ON DELETE SET NULL,
    ADD COLUMN backfilled_at     timestamptz;

ALTER TABLE chat_session
    ADD COLUMN agent_outcome  varchar(50),
    ADD COLUMN outcome_source varchar(20),
    ADD COLUMN eval_outcome   varchar(50),
    ADD COLUMN eval_status    varchar(20),
    ADD COLUMN eval_result_id uuid
        REFERENCES evaluation_result(id) ON DELETE SET NULL;

ALTER TABLE call_execution_config
    ADD COLUMN retry_policy varchar(20) NOT NULL DEFAULT 'LEGACY';

-- No indexes here, deliberately. These columns only exist once this file
-- runs, so their indexes cannot be built CONCURRENTLY ahead of it (the 054
-- pattern), and a plain CREATE INDEX inside this file would scan
-- lead_call_tracker while blocking every call write. Nothing reads the
-- columns in phase 1; the partial indexes ship with the phase 2 migration
-- that starts reading them, built CONCURRENTLY by hand after this one has
-- run (docs/CALL_OUTCOMES.md, section 6).
