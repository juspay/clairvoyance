-- Migration: Create template table and rename workflow columns
-- Description:
--   1. Create template table for storing workflow configurations
--   2. Rename workflow column to template in lead_call_tracker and call_execution_config tables
--   3. Add expected_payload_schema column to template table

-- Create template table
CREATE TABLE IF NOT EXISTS template (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR NOT NULL,
    shop_identifier VARCHAR,
    name VARCHAR NOT NULL,
    flow JSONB NOT NULL DEFAULT '{}',
    expected_payload_schema JSONB,
    expected_callback_response_schema JSONB,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Unique constraint for shop-specific templates (when shop_identifier IS NOT NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_template_shop_identifier_not_null
    ON template (merchant_id, shop_identifier, name)
    WHERE shop_identifier IS NOT NULL;

-- Unique constraint for generic templates (when shop_identifier IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_template_shop_identifier_is_null
    ON template (merchant_id, name)
    WHERE shop_identifier IS NULL;

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_template_merchant
    ON template(merchant_id);

CREATE INDEX IF NOT EXISTS idx_template_active
    ON template(is_active)
    WHERE is_active = true;

-- Rename workflow column to template in lead_call_tracker table
ALTER TABLE lead_call_tracker
RENAME COLUMN workflow TO template;

-- Rename workflow column to template in call_execution_config table
ALTER TABLE call_execution_config
RENAME COLUMN workflow TO template;
