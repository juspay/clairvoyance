-- Migration: Add merchant/shop to outbound_number and outbound_number_id to template
-- Description: Add merchant_id, shop_identifier to outbound_number table
--              Add outbound_number_id to template table to link templates with specific outbound numbers

-- Step 1: Add merchant_id and shop_identifier to outbound_number table
ALTER TABLE outbound_number
ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS shop_identifier VARCHAR(255);

-- Create index for merchant_id lookup
CREATE INDEX IF NOT EXISTS idx_outbound_number_merchant_id
    ON outbound_number (merchant_id);

-- Create index for merchant and shop combination
CREATE INDEX IF NOT EXISTS idx_outbound_number_merchant_shop
    ON outbound_number (merchant_id, shop_identifier);

-- Add comments for documentation
COMMENT ON COLUMN outbound_number.merchant_id IS
    'Merchant identifier for this outbound number. Used to associate numbers with specific merchants.';

COMMENT ON COLUMN outbound_number.shop_identifier IS
    'Shop identifier for this outbound number.';

-- Step 2: Add outbound_number_id to template table
ALTER TABLE template
ADD COLUMN IF NOT EXISTS outbound_number_id UUID;


-- Create index for outbound_number_id lookup
CREATE INDEX IF NOT EXISTS idx_template_outbound_number_id
    ON template (outbound_number_id);

-- Add comment for documentation
COMMENT ON COLUMN template.outbound_number_id IS
    'Reference to the outbound number used for this template. NULL means no specific number assigned.';

ALTER TABLE outbound_number drop constraint if exists outbound_number_number_key;