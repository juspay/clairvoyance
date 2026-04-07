-- Migration 025: Add merchant_identifier index to call_execution_config
-- Description:
--   Add index for merchant-only queries on call_execution_config
--
-- Background:
--   This index improves query performance for lookups by merchant_id only.
--   Created as a separate migration since the original migration (018) was already applied.

CREATE INDEX IF NOT EXISTS idx_call_execution_config_merchant_id
    ON call_execution_config(merchant_id);
