-- Migration: Add pre_checks column and create credentials table
-- Description: 
--   1. Adds a JSONB column to call_execution_config for pre-check configurations
--      (e.g., external API checks) that run before a call is initiated.
--   2. Creates credentials table for centralized credential storage with optional encryption.
--      Credentials are global (merchant_id NULL) or merchant-scoped.
--      Used for API keys, tokens, and other secrets needed by templates, hooks, and pre-checks.

-- ============================================================================
-- 1. Add pre_checks column to call_execution_config
-- ============================================================================
ALTER TABLE call_execution_config
    ADD COLUMN IF NOT EXISTS pre_checks JSONB DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_call_execution_config_merchant_id
    ON call_execution_config(merchant_id);

-- ============================================================================
-- 2. Create credentials table
-- ============================================================================
CREATE TABLE IF NOT EXISTS credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255),
    name VARCHAR(255) NOT NULL,
    credential_type VARCHAR(50) NOT NULL CHECK (credential_type IN ('api_key', 'bearer_token', 'basic_auth', 'custom')),
    value TEXT NOT NULL,
    is_encrypted BOOLEAN DEFAULT FALSE,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Unique constraint: one credential name per merchant (merchant-scoped)
CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_merchant_name
    ON credentials(merchant_id, name)
    WHERE merchant_id IS NOT NULL;

-- Unique constraint: one credential name for globals (merchant_id IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_global_name
    ON credentials(name)
    WHERE merchant_id IS NULL;

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_credentials_merchant_id ON credentials(merchant_id);
CREATE INDEX IF NOT EXISTS idx_credentials_is_active ON credentials(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_credentials_credential_type ON credentials(credential_type);
