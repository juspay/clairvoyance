-- Migration: partial indexes for the call outcome columns (migration 080)
-- Description: phase 2 starts reading connection_status, agent_outcome and
-- eval_outcome (new analytics types, filters), so they get the indexes 080
-- deliberately left out. Partial on IS NOT NULL: rows written before the
-- columns existed (and not backfilled) are never indexed.
--
-- NOTE ON LOCKING: the migration runner wraps each file in a transaction, so
-- CREATE INDEX CONCURRENTLY cannot be used here, and a plain CREATE INDEX
-- scans lead_call_tracker while blocking writes. On production, run the
-- CONCURRENTLY forms manually FIRST (outside a transaction) and then apply
-- this migration -- IF NOT EXISTS makes each a no-op (054 precedent). This
-- works here because 080 already created the columns.
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_connection_status
--       ON lead_call_tracker (connection_status)
--       WHERE connection_status IS NOT NULL;
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_agent_outcome
--       ON lead_call_tracker (agent_outcome)
--       WHERE agent_outcome IS NOT NULL;
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_eval_outcome
--       ON lead_call_tracker (eval_outcome)
--       WHERE eval_outcome IS NOT NULL;
--
-- Plan: docs/CALL_OUTCOMES.md (Phase 2, 2c).

CREATE INDEX IF NOT EXISTS idx_lct_connection_status
    ON lead_call_tracker (connection_status)
    WHERE connection_status IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lct_agent_outcome
    ON lead_call_tracker (agent_outcome)
    WHERE agent_outcome IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lct_eval_outcome
    ON lead_call_tracker (eval_outcome)
    WHERE eval_outcome IS NOT NULL;
