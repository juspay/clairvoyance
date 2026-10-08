-- Migration: call outcome facts (platform / provider / session / agent / eval)
-- Description: lead_call_tracker.outcome is written by about thirty places —
-- the dispatcher (PRECHECK_FAILED, BLACKLISTED, ...), the carrier callback
-- (NO_ANSWER for every failure), the pipeline's own fallbacks (BUSY on idle
-- timeout / hangup, UNKNOWN, EARLY_HANGUP, TRANSFERRED) and the agent — and
-- the last writer wins. These columns record the FACTS instead, each named by
-- who writes it:
--
--   platform  platform_status (INITIATED / NOT_INITIATED), platform_reason
--             (DIALED / WEB_SESSION, or today's refusal word) — was the call
--             set up, and how, or why not
--   provider  provider_status (ANSWERED / NOT_ANSWERED / UNKNOWN),
--             provider_reason (COMPLETED, or why it went unanswered),
--             provider_hangup_cause (the provider's own cause, diagnostics)
--             — what happened on the phone line; empty on a web session
--   session   session_end_reason — how an answered session ended
--   agent     agent_outcome (the agent's word, exactly as the legacy column
--             stores it), agent_outcome_source (LLM / IVR / OBSERVER)
--   eval      eval_outcome, eval_status, eval_result_id — the post-call
--             eval; created here, filled by the eval engine
--
-- One pure function, legacy_outcome(facts) in app/schemas/breeze_buddy/
-- outcomes.py, turns the facts into today's outcome word. PR 1 records them
-- beside the legacy writes and compares the two on every terminal write; PR 2
-- removes the legacy writes. Writes are gated by the
-- CALL_OUTCOME_WRITES_ENABLED dynamic flag (default off), so the code may
-- ship before this migration runs.
--
-- Every column is nullable with no default: ADD COLUMN is metadata-only, no
-- table rewrite. Vocabulary lives in code, never in CHECKs.
--
-- eval_result_id is a plain uuid for now, deliberately not a foreign key to
-- evaluation_result: that table cascades from template, so a template delete
-- would fire the FK's ON DELETE action once per evaluation result, each a
-- full scan of lead_call_tracker / chat_session without an index on the
-- column. The FK ships with its index (built CONCURRENTLY by hand first) once
-- the eval engine starts writing the column.
--
-- chat_session gets the agent and eval layers only: a chat has no platform
-- set-up or phone line, and its ended_reason already records how a session
-- ended.
--
-- No indexes: nothing filters on these columns yet. If a reader needs one,
-- it is built CONCURRENTLY by hand first and shipped as a no-op migration
-- (the 054 pattern).
--
-- Plan: docs/CALL_OUTCOMES.md.

ALTER TABLE lead_call_tracker
    ADD COLUMN platform_status       varchar(20),
    ADD COLUMN platform_reason       varchar(30),
    ADD COLUMN provider_status       varchar(20),
    ADD COLUMN provider_reason       varchar(20),
    ADD COLUMN provider_hangup_cause varchar(100),
    ADD COLUMN session_end_reason    varchar(30),
    ADD COLUMN agent_outcome         varchar(50),
    ADD COLUMN agent_outcome_source  varchar(20),
    ADD COLUMN eval_outcome          varchar(50),
    ADD COLUMN eval_status           varchar(20),
    ADD COLUMN eval_result_id        uuid;

ALTER TABLE chat_session
    ADD COLUMN agent_outcome        varchar(50),
    ADD COLUMN agent_outcome_source varchar(20),
    ADD COLUMN eval_outcome         varchar(50),
    ADD COLUMN eval_status          varchar(20),
    ADD COLUMN eval_result_id       uuid;
