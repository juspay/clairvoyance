-- Migration: Add unique constraint for merchant+template deduplication
-- Purpose: Enable proper deduplication to prevent duplicate config entries
-- when merchants toggle settings on/off or update their configurations.

-- Step 1: Add unique constraint on (merchant_id, template)
-- This prevents duplicate configs for the same merchant + template combination
-- and enables ON CONFLICT upsert behavior in the INSERT query
ALTER TABLE call_execution_config 
    ADD CONSTRAINT uq_call_execution_config_merchant_template 
    UNIQUE (merchant_id, template);

-- Step 2: Handle reseller-level default configs where merchant_id IS NULL
-- The UNIQUE constraint above doesn't prevent duplicates when merchant_id is NULL
-- (because NULL != NULL in SQL). This partial index ensures one default config
-- per reseller + template combination.
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_reseller_template_null_merchant
    ON call_execution_config (reseller_id, template)
    WHERE merchant_id IS NULL;
