-- Migration: The agent's own outcome word, and an index for failed evals
-- Description: Two pieces the evals and their analytics need. Only the
-- end-of-call outcome check (outcome_correctness) ever replaces a call's
-- outcome; the custom evals run after the call only store results.
--
-- 1. lead_call_tracker.agent_outcome: the agent's own outcome word. Every
-- outcome write sets it with `outcome` (the agent's outcome hook, the call's
-- completion, an abort, a blocked call's insert) but the end-of-call outcome
-- check's (conversation_analysis/preset/outcome_eval.py), which replaces
-- `outcome` alone. So `outcome` is the call's final word and `agent_outcome`
-- what the agent said; they differ only where the check replaced it, and the
-- verdict that decided it is the call's evaluation_result row (source_id =
-- id, result = 'outcome_correctness').
--
-- Leads with an outcome from before this column are backfilled with it by
-- hand, not by this migration (a. below). Where the check had already
-- replaced a lead's outcome, that is the check's word; the agent's is only in
-- the check's log line (component buddy.outcome_eval, result=replaced).
--
-- Nullable with no default: ADD COLUMN is metadata-only, no table rewrite.
-- Same width as `outcome`, whose word it holds. No index on it: a plain
-- CREATE INDEX on lead_call_tracker blocks writes; build one CONCURRENTLY by
-- hand if analytics need it.
--
-- 2. evaluation_result_failed_time. Eval analytics count an agent's failed
-- evaluations over a date range: the outcome check's timeouts and failures,
-- and custom evals that kept failing (FAILED rows, written once per call per
-- eval). Every other evaluation_result index covers COMPLETED rows only, so
-- without this that count scans the whole table. FAILED rows are few: the
-- index is small and cheap to keep.
--
-- NOTE ON LOCKING: the migration runner wraps each file in a transaction, so
-- every lock is held until the whole file commits.
--
-- a. The backfill is not in this file. ADD COLUMN takes an ACCESS EXCLUSIVE
-- lock on lead_call_tracker until the file commits, and an UPDATE here would
-- scan the whole table under it, blocking every read and write. After this
-- migration, once the pods from before it are gone, backfill by hand, a day
-- of leads per transaction (idx_lead_call_tracker_created_at) until none is
-- left:
--
--   UPDATE lead_call_tracker SET agent_outcome = outcome
--   WHERE created_at >= '<day>' AND created_at < '<day>'::date + 1
--     AND agent_outcome IS NULL AND outcome IS NOT NULL;
--
-- b. The index. CREATE INDEX CONCURRENTLY cannot run in a transaction, and a
-- plain CREATE INDEX blocks writes to evaluation_result while it builds. On a
-- large production table, run the CONCURRENTLY form by hand FIRST:
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS evaluation_result_failed_time
--       ON evaluation_result (template_id, evaluation_type, started_at DESC)
--       WHERE status = 'FAILED';
--
-- A failed concurrent build leaves an INVALID index of this name, which IF
-- NOT EXISTS below would accept; DROP INDEX CONCURRENTLY it and re-run
-- before applying.
--
-- Then apply this migration: the index already exists, and ADD COLUMN
-- (nullable, no default) only changes the catalog, so its lock is brief. The
-- index comes first in this file, so if it was not built by hand, it builds
-- before ADD COLUMN locks lead_call_tracker, not while that lock is held.

CREATE INDEX IF NOT EXISTS evaluation_result_failed_time
    ON evaluation_result (template_id, evaluation_type, started_at DESC)
    WHERE status = 'FAILED';

ALTER TABLE lead_call_tracker ADD COLUMN IF NOT EXISTS agent_outcome varchar(50);

COMMENT ON COLUMN lead_call_tracker.agent_outcome IS
    'The agent''s own outcome word, set with every outcome write but the '
    'end-of-call outcome check''s (outcome_correctness): it differs from '
    'outcome only where the check replaced it.';
