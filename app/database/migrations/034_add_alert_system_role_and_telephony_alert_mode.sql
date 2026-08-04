-- 034_add_alert_system_role_and_telephony_alert_mode.sql
-- 1. Add 'alert_system' to the users role CHECK constraint.
-- 2. Add 'TELEPHONY_ALERT' to the lead_call_tracker execution_mode CHECK constraint.

BEGIN;

-- Users role: add 'alert_system'
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('admin', 'reseller', 'merchant', 'user', 'alert_system'));

-- Execution mode: add 'TELEPHONY_ALERT'
ALTER TABLE lead_call_tracker DROP CONSTRAINT IF EXISTS lead_call_tracker_execution_mode_check;
ALTER TABLE lead_call_tracker ADD CONSTRAINT lead_call_tracker_execution_mode_check
    CHECK (execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST', 'DAILY', 'DAILY_TEST', 'DAILY_STREAM', 'HOLD_TRANSFER', 'TELEPHONY_ALERT'));

COMMIT;
