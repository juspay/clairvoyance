-- Migration 032: Add partial covering index for analytics summary queries
-- Description:
--   Adds a partial covering index on lead_call_tracker that includes every
--   column touched by get_analytics_summary_query() in aggregate mode.
--   Enables a pure Index Only Scan for the call-based analytics endpoint,
--   eliminating heap access entirely for count cards.
--
-- Why partial:
--   The WHERE clause in analytics queries always filters execution_mode to
--   ('TELEPHONY', 'HOLD_TRANSFER'). A partial index is smaller and cheaper
--   to maintain than a full index.
--
-- Columns:
--   Key:   execution_mode, call_initiated_time, call_direction, outcome, status
--   Include: template, merchant_id, call_end_time

BEGIN;

CREATE INDEX IF NOT EXISTS idx_lct_analytics_covering
    ON lead_call_tracker (execution_mode, call_initiated_time, call_direction, outcome, status)
    INCLUDE (template, merchant_id, call_end_time)
    WHERE execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER');

COMMIT;
