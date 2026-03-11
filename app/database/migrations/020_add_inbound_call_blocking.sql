-- Migration: Add inbound call blocking columns to call_execution_config
-- Description: Per-template inbound call control on call_execution_config:
--   - enable_inbound: master toggle
--   - Business hours (inbound_call_start_time / inbound_call_end_time)
--   - Block action (REJECT or REDIRECT) with message and redirect number

-- Master toggle for inbound calls (TRUE = inbound allowed)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS enable_inbound BOOLEAN DEFAULT TRUE;

-- Business hours for inbound calls (NULL = always open)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_call_start_time TIME,
ADD COLUMN IF NOT EXISTS inbound_call_end_time TIME;

-- What to do when an inbound call is blocked (REJECT or REDIRECT)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_block_action VARCHAR(20) DEFAULT 'REJECT'
    CHECK (inbound_block_action IN ('REJECT', 'REDIRECT'));

-- Where to redirect blocked calls (used when inbound_block_action = 'REDIRECT')
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_redirect_number VARCHAR(20);

-- TTS message played before hanging up or redirecting
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_block_message TEXT;

COMMENT ON COLUMN call_execution_config.enable_inbound IS
    'Per-template inbound toggle. When FALSE, inbound calls are blocked with inbound_block_action.';
COMMENT ON COLUMN call_execution_config.inbound_call_start_time IS
    'Start of business hours for inbound calls (inclusive). NULL means no time restriction.';
COMMENT ON COLUMN call_execution_config.inbound_call_end_time IS
    'End of business hours for inbound calls (inclusive). NULL means no time restriction.';
COMMENT ON COLUMN call_execution_config.inbound_block_action IS
    'Action when inbound call is blocked: REJECT (hang up with message) or REDIRECT (transfer to another number).';
COMMENT ON COLUMN call_execution_config.inbound_redirect_number IS
    'Phone number to redirect to when inbound_block_action is REDIRECT.';
COMMENT ON COLUMN call_execution_config.inbound_block_message IS
    'TTS message played to caller before blocking/redirecting. NULL uses a default message.';
