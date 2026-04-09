-- Migration 023: Create outbound_number_pool table and add pool_id to outbound_number
-- This enables grouping outbound numbers into pools with shared channel limits and round-robin rotation.

-- Create the pool table
CREATE TABLE IF NOT EXISTS outbound_number_pool (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    provider VARCHAR(50) CHECK (provider IN ('TWILIO', 'EXOTEL', 'PLIVO')) NOT NULL,
    reseller_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255),
    max_channels INTEGER NOT NULL,
    current_channels INTEGER NOT NULL DEFAULT 0,
    rotation_index INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(50) CHECK (status IN ('ACTIVE', 'DISABLED')) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes for pool lookup
CREATE INDEX IF NOT EXISTS idx_outbound_number_pool_reseller_id ON outbound_number_pool (reseller_id);
CREATE INDEX IF NOT EXISTS idx_outbound_number_pool_reseller_merchant ON outbound_number_pool (reseller_id, merchant_id);
CREATE INDEX IF NOT EXISTS idx_outbound_number_pool_status ON outbound_number_pool (status);

-- Add pool_id column to outbound_number table
ALTER TABLE outbound_number ADD COLUMN IF NOT EXISTS pool_id VARCHAR(255) REFERENCES outbound_number_pool(id) ON DELETE SET NULL;

-- Index for looking up numbers belonging to a pool
CREATE INDEX IF NOT EXISTS idx_outbound_number_pool_id ON outbound_number (pool_id);
