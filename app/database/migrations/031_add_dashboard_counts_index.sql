-- Migration 031: Add covering index for dashboard count queries
-- Description:
--   Adds a composite index on lead_call_tracker optimized for the dashboard
--   card count queries. Enables index-only scans for filtered COUNT(*)
--   operations over date ranges.
--
-- Columns ordered for:
--   1. execution_mode (equality: TELEPHONY, HOLD_TRANSFER)
--   2. call_initiated_time (range: date_from .. date_to)
--   3. call_direction (equality: OUTBOUND, INBOUND)
--   4. outcome (equality: NO_ANSWER, BUSY, etc.)
--   5. status (equality: FINISHED, etc.)

BEGIN;

CREATE INDEX IF NOT EXISTS idx_lct_dashboard_counts
    ON lead_call_tracker (execution_mode, call_initiated_time, call_direction, outcome, status);

COMMIT;
