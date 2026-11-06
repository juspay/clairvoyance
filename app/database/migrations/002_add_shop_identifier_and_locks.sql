-- Migration: Add shop identifier and locking mechanism
-- Description: Add shop_identifier, is_locked, and enable_international_call fields

-- Add new columns to lead_call_tracker
ALTER TABLE lead_call_tracker 
    ADD COLUMN IF NOT EXISTS shop_identifier VARCHAR(255),
    ADD COLUMN IF NOT EXISTS is_locked BOOLEAN DEFAULT FALSE NOT NULL;

-- Add new columns to call_execution_config
ALTER TABLE call_execution_config 
    ADD COLUMN IF NOT EXISTS shop_identifier VARCHAR(255),
    ADD COLUMN IF NOT EXISTS enable_international_call BOOLEAN DEFAULT TRUE;

-- Drop old unique constraint on call_execution_config
ALTER TABLE call_execution_config 
    DROP CONSTRAINT IF EXISTS call_execution_config_merchant_id_workflow_key;

-- Create new unique indexes that handle shop_identifier
-- One for shop-specific configs (when shop_identifier IS NOT NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_shop
    ON call_execution_config (merchant_id, workflow, shop_identifier)
    WHERE shop_identifier IS NOT NULL;

-- One for generic configs (when shop_identifier IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_generic
    ON call_execution_config (merchant_id, workflow)
    WHERE shop_identifier IS NULL;

-- Add performance indexes for locking queries
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_is_locked 
    ON lead_call_tracker (is_locked);
    
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_status_next_attempt_locked 
    ON lead_call_tracker (status, is_locked, next_attempt_at);
