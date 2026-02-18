-- Migration: Create credentials table
-- Description: Centralized credential storage with optional KMS encryption.
-- Credentials are global (merchant_id NULL) or merchant-scoped.
-- Used for API keys, tokens, and other secrets needed by templates, hooks, and pre-checks.

CREATE TABLE IF NOT EXISTS credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255),
    name VARCHAR(255) NOT NULL,
    credential_type VARCHAR(50) NOT NULL CHECK (credential_type IN ('api_key', 'bearer_token', 'basic_auth', 'custom')),
    value TEXT NOT NULL,
    is_encrypted BOOLEAN DEFAULT FALSE,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
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
