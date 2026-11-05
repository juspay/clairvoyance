-- Migration 035: Add dedicated out-of-hours inbound TTS message
--
-- inbound_block_message is used for generic inbound blocking (rate limit,
-- blacklist, disabled inbound, etc.). Add a dedicated message for
-- outside-business-hours so merchants can keep separate copy.

BEGIN;

ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_outside_hours_message TEXT;

COMMENT ON COLUMN call_execution_config.inbound_outside_hours_message IS
    'TTS message played when inbound call arrives outside inbound_call_start_time/inbound_call_end_time. Falls back to inbound_block_message when NULL.';

COMMIT;
