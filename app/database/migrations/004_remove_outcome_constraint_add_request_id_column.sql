-- Migration: Remove outcome CHECK constraint from lead_call_tracker
-- Description: Drop the CHECK constraint that limits outcome values to specific enums

-- Drop the CHECK constraint on outcome column
ALTER TABLE lead_call_tracker
DROP CONSTRAINT IF EXISTS lead_call_tracker_outcome_check;

ALTER TABLE lead_call_tracker
DROP CONSTRAINT IF EXISTS lead_call_tracker_workflow_check;

ALTER TABLE call_execution_config
DROP CONSTRAINT IF EXISTS call_execution_config_workflow_check;

-- Description: Move order_id from payload JSONB to a dedicated request_id column for better querying
-- Add request_id column to lead_call_tracker table

ALTER TABLE lead_call_tracker
ADD COLUMN IF NOT EXISTS request_id VARCHAR(255);

-- Create index on request_id for faster lookups
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_request_id
    ON lead_call_tracker (request_id);

-- Migrate existing data from payload->>'order_id' to request_id column
UPDATE lead_call_tracker
SET request_id = payload->>'order_id'
WHERE payload IS NOT NULL AND payload->>'order_id' IS NOT NULL;

