-- Migration: Add DAILY_STREAM to execution_mode CHECK constraint
-- Description: Allows STT/TTS-only Daily sessions without LLM

-- Drop old constraint and re-create with DAILY_STREAM included
ALTER TABLE lead_call_tracker
DROP CONSTRAINT IF EXISTS lead_call_tracker_execution_mode_check;

ALTER TABLE lead_call_tracker
ADD CONSTRAINT lead_call_tracker_execution_mode_check
CHECK (execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST', 'DAILY', 'DAILY_TEST', 'DAILY_STREAM'));
