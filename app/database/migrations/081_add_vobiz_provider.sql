-- Migration: Add VOBIZ provider support
-- Description: Add VOBIZ to the provider check constraints on telephony_numbers
-- and call_execution_config (same shape as 013_add_plivo_provider.sql).

ALTER TABLE telephony_numbers
    DROP CONSTRAINT IF EXISTS telephony_numbers_provider_check;

ALTER TABLE telephony_numbers
    ADD CONSTRAINT telephony_numbers_provider_check
    CHECK (provider IN ('TWILIO', 'EXOTEL', 'PLIVO', 'VOBIZ'));

ALTER TABLE call_execution_config
    DROP CONSTRAINT IF EXISTS call_execution_config_calling_provider_check;

ALTER TABLE call_execution_config
    ADD CONSTRAINT call_execution_config_calling_provider_check
    CHECK (calling_provider IN ('TWILIO', 'EXOTEL', 'PLIVO', 'VOBIZ'));
