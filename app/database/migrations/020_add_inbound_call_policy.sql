-- Migration: Add inbound call policy columns to call_execution_config
-- Description: Unified per-template inbound call control:
--   - enable_inbound: master toggle
--   - Business hours with configurable timezone
--   - Block action (REJECT or REDIRECT) with message and redirect number
--   - Blacklist enforcement toggle
--   - Rate limiting (fixed-window via Redis, config stored here)

-- Master toggle for inbound calls (TRUE = inbound allowed)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS enable_inbound BOOLEAN DEFAULT TRUE;

-- Business hours for inbound calls (NULL = always open)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_call_start_time TIME,
ADD COLUMN IF NOT EXISTS inbound_call_end_time TIME;

-- Timezone for business hours (NULL = Asia/Kolkata default)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS inbound_call_timezone VARCHAR(50);

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

-- Whether to check caller against blacklist
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS enforce_blacklist BOOLEAN DEFAULT TRUE;

-- Rate limiting: fixed-window counter config (enforcement via Redis INCR+EXPIRE)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS rate_limit_enabled BOOLEAN DEFAULT FALSE;

ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS rate_limit_max_calls INTEGER CHECK (rate_limit_max_calls > 0);

ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS rate_limit_window_seconds INTEGER DEFAULT 1800 CHECK (rate_limit_window_seconds > 0);

-- Comma-separated phone numbers exempt from rate limiting (for testing)
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS rate_limit_whitelist TEXT;

COMMENT ON COLUMN call_execution_config.enable_inbound IS
    'Per-template inbound toggle. When FALSE, inbound calls are blocked with inbound_block_action.';
COMMENT ON COLUMN call_execution_config.inbound_call_start_time IS
    'Start of business hours for inbound calls (inclusive). NULL means no time restriction.';
COMMENT ON COLUMN call_execution_config.inbound_call_end_time IS
    'End of business hours for inbound calls (inclusive). NULL means no time restriction.';
COMMENT ON COLUMN call_execution_config.inbound_call_timezone IS
    'IANA timezone for business hours (e.g. Asia/Kolkata, America/New_York). NULL defaults to Asia/Kolkata.';
COMMENT ON COLUMN call_execution_config.inbound_block_action IS
    'Action when inbound call is blocked: REJECT (hang up with message) or REDIRECT (transfer to another number).';
COMMENT ON COLUMN call_execution_config.inbound_redirect_number IS
    'Phone number to redirect to when inbound_block_action is REDIRECT.';
COMMENT ON COLUMN call_execution_config.inbound_block_message IS
    'TTS message played to caller before blocking/redirecting. NULL uses a default message.';
COMMENT ON COLUMN call_execution_config.enforce_blacklist IS
    'Whether to check caller number against the blacklist table for this template.';
COMMENT ON COLUMN call_execution_config.rate_limit_enabled IS
    'Whether to enforce inbound call rate limiting for this template.';
COMMENT ON COLUMN call_execution_config.rate_limit_max_calls IS
    'Maximum inbound calls allowed within rate_limit_window_seconds.';
COMMENT ON COLUMN call_execution_config.rate_limit_window_seconds IS
    'Fixed-window duration in seconds for rate limiting (default 1800 = 30 min).';
COMMENT ON COLUMN call_execution_config.rate_limit_whitelist IS
    'Comma-separated phone numbers exempt from rate limiting (e.g. +919876543210,+919876543211).';
