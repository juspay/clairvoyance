-- Migration: Add pre_checks column to call_execution_config
-- Description: Adds a JSONB column to store pre-check configurations (e.g., external API checks)
-- that run before a call is initiated. Pre-checks are per merchant/template/shop.

ALTER TABLE call_execution_config
    ADD COLUMN IF NOT EXISTS pre_checks JSONB DEFAULT NULL;
