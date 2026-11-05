-- Migration 019: Create merchants table and add owner_id to users
-- This separates business entities (merchants table) from login accounts (users table)

BEGIN;

-- ============================================================================
-- 1. Create merchants table (business entities)
-- ============================================================================
CREATE TABLE IF NOT EXISTS merchants (
    merchant_identifier VARCHAR(255) PRIMARY KEY,       -- Business identifier (e.g., "redbus")
    name VARCHAR(255),                          -- Optional display name (e.g., "RedBus India")
    description TEXT,                           -- Optional description
    is_active BOOLEAN DEFAULT true,             -- Active status (for future use)
    reseller_id VARCHAR(255),                   -- Which reseller owns this merchant (references users.id where role='reseller')
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Indexes for merchants table
CREATE INDEX IF NOT EXISTS idx_merchants_reseller_id ON merchants(reseller_id);
CREATE INDEX IF NOT EXISTS idx_merchants_is_active ON merchants(is_active);
CREATE INDEX IF NOT EXISTS idx_merchants_created_at ON merchants(created_at);

-- ============================================================================
-- 2. Add owner_id column to users table
-- ============================================================================
ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_users_owner_id ON users(owner_id);

-- ============================================================================
-- 3. Update role CHECK constraint: rename 'shop' to 'user'
-- ============================================================================
-- Drop old constraint
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;

-- Migrate existing 'shop' roles to 'user' BEFORE adding new constraint
UPDATE users SET role = 'user' WHERE role = 'shop';

-- Add new constraint with 'user' instead of 'shop'
ALTER TABLE users ADD CONSTRAINT users_role_check 
    CHECK (role IN ('admin', 'reseller', 'merchant', 'user'));

-- ============================================================================
-- 4. Change users.id from UUID to human-readable VARCHAR(255)
--    Existing UUID values remain valid as VARCHAR strings.
-- ============================================================================
ALTER TABLE users ALTER COLUMN id TYPE VARCHAR(255);
ALTER TABLE users ALTER COLUMN id DROP DEFAULT;
ALTER TABLE users ADD CONSTRAINT users_id_no_spaces CHECK (id !~ '\s');

COMMIT;
