-- Migration: Create users table for JWT-based authentication with multi-shop and multi-merchant RBAC
-- Description: Add users table with shop_identifiers and merchant_ids for hierarchical Role-Based Access Control
-- Date: 2025-12-17

BEGIN;

-- Create users table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'reseller', 'merchant', 'shop')),
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,

    -- Multi-merchant access control
    -- ["*"] = all merchants (admin/reseller)
    -- ["merchant_123", "merchant_456"] = specific merchants
    -- [] = no merchant-level access
    merchant_ids JSONB DEFAULT '[]'::jsonb NOT NULL,

    -- Multi-shop access control
    -- ["*"] = all shops (admin/reseller/merchant with wildcard)
    -- ["shop_123", "shop_456"] = specific shops
    -- [] = no shop-level access
    shop_identifiers JSONB DEFAULT '[]'::jsonb NOT NULL,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_merchant_ids ON users USING GIN(merchant_ids);
CREATE INDEX IF NOT EXISTS idx_users_shop_identifiers ON users USING GIN(shop_identifiers);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Add comments for documentation
COMMENT ON TABLE users IS 'User accounts with hierarchical role-based access control (merchant + shop levels)';
COMMENT ON COLUMN users.merchant_ids IS 'JSONB array of merchant IDs. ["*"] for all merchants, specific IDs like ["merchant_123"], or [] for no access';
COMMENT ON COLUMN users.shop_identifiers IS 'JSONB array of shop identifiers. ["*"] for all shops, specific IDs like ["shop_123", "shop_456"], or [] for no access';
COMMENT ON COLUMN users.role IS 'User role: admin (all access), reseller (managed merchants/shops), merchant (owned shops), shop (single shop)';

COMMIT;
