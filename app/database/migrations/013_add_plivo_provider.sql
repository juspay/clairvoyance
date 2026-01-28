-- Migration: Add PLIVO provider support
-- Description: Add PLIVO to the provider check constraints in outbound_number and call_execution_config tables

-- Update outbound_number table provider constraint
ALTER TABLE outbound_number
    DROP CONSTRAINT IF EXISTS outbound_number_provider_check;

ALTER TABLE outbound_number
    ADD CONSTRAINT outbound_number_provider_check
    CHECK (provider IN ('TWILIO', 'EXOTEL', 'PLIVO'));

-- Update call_execution_config table calling_provider constraint
ALTER TABLE call_execution_config
    DROP CONSTRAINT IF EXISTS call_execution_config_calling_provider_check;

ALTER TABLE call_execution_config
    ADD CONSTRAINT call_execution_config_calling_provider_check
    CHECK (calling_provider IN ('TWILIO', 'EXOTEL', 'PLIVO'));
