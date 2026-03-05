-- Migration: Create blacklisted_numbers table
-- Description: Table to block specific phone numbers from being called

CREATE TABLE IF NOT EXISTS blacklisted_numbers (
    id VARCHAR(255) PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    merchant_id VARCHAR(255) DEFAULT NULL,
    reason VARCHAR(500) DEFAULT NULL,
    created_by VARCHAR(255) DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Unique constraint for per-merchant blacklist entries
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone_merchant
    ON blacklisted_numbers (phone_number, merchant_id)
    WHERE merchant_id IS NOT NULL;

-- Unique constraint for global blacklist entries (NULL merchant_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone_global
    ON blacklisted_numbers (phone_number)
    WHERE merchant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone_number
    ON blacklisted_numbers (phone_number);
CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_merchant_id
    ON blacklisted_numbers (merchant_id);
