-- Add telephony_config JSONB column to call_execution_config table.
-- This allows per-merchant overrides of telephony provider settings
-- (e.g. applet_app_id) that would otherwise come from env defaults.

ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS telephony_config JSONB DEFAULT NULL;
