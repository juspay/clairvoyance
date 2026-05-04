-- Migration: Add HOLD_TRANSFER to execution_mode CHECK constraint
-- Description: Allows outbound legs of hold & consultative transfers to be
--              stored with a distinct execution_mode so they are excluded from
--              cron pickup and retry logic while still appearing in analytics.

-- Drop old constraint and re-create with HOLD_TRANSFER included
ALTER TABLE lead_call_tracker
DROP CONSTRAINT IF EXISTS lead_call_tracker_execution_mode_check;

ALTER TABLE lead_call_tracker
ADD CONSTRAINT lead_call_tracker_execution_mode_check
CHECK (execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST', 'DAILY', 'DAILY_TEST', 'DAILY_STREAM', 'HOLD_TRANSFER'));
