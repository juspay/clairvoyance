-- Migration: Add execution_mode column to lead_call_tracker
-- Description: Separates production calls from test calls across transport types
--              Values: TELEPHONY, TELEPHONY_TEST, DAILY, DAILY_TEST

-- Add execution_mode column with default value
-- DEFAULT 'TELEPHONY' will automatically set all existing rows to 'TELEPHONY'
ALTER TABLE lead_call_tracker
ADD COLUMN execution_mode VARCHAR(20) DEFAULT 'TELEPHONY' NOT NULL;

-- Add CHECK constraint for valid values
ALTER TABLE lead_call_tracker
ADD CONSTRAINT lead_call_tracker_execution_mode_check
CHECK (execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST', 'DAILY', 'DAILY_TEST'));

-- Create index for efficient filtering (cron, analytics)
CREATE INDEX idx_lead_call_tracker_execution_mode
ON lead_call_tracker (execution_mode);
