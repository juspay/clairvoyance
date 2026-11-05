-- Migration: Add enable_calling column to call_execution_config
-- Description: Add enable_calling boolean field to control whether calls can be initiated

-- Add enable_calling column to call_execution_config
ALTER TABLE call_execution_config
    ADD COLUMN IF NOT EXISTS enable_calling BOOLEAN DEFAULT TRUE NOT NULL;

-- Add index for performance
CREATE INDEX IF NOT EXISTS idx_call_execution_config_enable_calling
    ON call_execution_config (enable_calling);
