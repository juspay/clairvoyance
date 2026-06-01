-- Migration: Add call_direction column to lead_call_tracker
-- Description: Tracks whether call was inbound or outbound
--              OUTBOUND = We called customer
--              INBOUND = Customer called us
-- Note: DEFAULT 'OUTBOUND' will automatically set all existing rows to 'OUTBOUND'

-- Add call_direction column with default value
ALTER TABLE lead_call_tracker
ADD COLUMN call_direction VARCHAR(20) DEFAULT 'OUTBOUND' NOT NULL;

-- Add CHECK constraint for valid values
ALTER TABLE lead_call_tracker
ADD CONSTRAINT lead_call_tracker_call_direction_check
CHECK (call_direction IN ('INBOUND', 'OUTBOUND'));

-- Create index for efficient filtering
CREATE INDEX idx_lead_call_tracker_call_direction
ON lead_call_tracker (call_direction);
