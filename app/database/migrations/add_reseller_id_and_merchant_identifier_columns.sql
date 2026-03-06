-- filepath: /Users/sharifajahan.shaik/Desktop/clairvoyance/app/database/migrations/009_add_reseller_id_and_merchant_identifier_columns.sql
-- Migration: Add reseller_id and merchant_identifier columns (backward compatible)
-- Description:
--   1. Add reseller_id column to tables (copy from merchant_id)
--   2. Add merchant_identifier column to tables (copy from shop_identifier)
--   3. Add reseller_ids column to users (copy from merchant_ids)
--   4. Add merchant_identifiers column to users (copy from shop_identifiers)
--
-- Background:
--   Standardizing naming convention while maintaining backward compatibility.
--   Old columns (merchant_id, shop_identifier) are kept for backward compatibility.
--   New columns (reseller_id, merchant_identifier) are added and populated.

-- =====================================================
-- 1. Add reseller_id to call_execution_config
-- =====================================================
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE call_execution_config
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- =====================================================
-- 2. Add merchant_identifier to call_execution_config
-- =====================================================
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS merchant_identifier VARCHAR(255);

UPDATE call_execution_config
SET merchant_identifier = shop_identifier
WHERE merchant_identifier IS NULL AND shop_identifier IS NOT NULL;

-- Composite index for queries filtering by (reseller_id, merchant_identifier, template)
CREATE INDEX IF NOT EXISTS idx_call_execution_config_reseller_merchant_template
    ON call_execution_config(reseller_id, merchant_identifier, template);

-- Individual index for reseller-only queries (fallback scenarios)
CREATE INDEX IF NOT EXISTS idx_call_execution_config_reseller_id
    ON call_execution_config(reseller_id);

-- =====================================================
-- 3. Add reseller_id to lead_call_tracker
-- =====================================================
ALTER TABLE lead_call_tracker
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE lead_call_tracker
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- Individual index (analytics uses COALESCE with OR, not AND)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_reseller_id
    ON lead_call_tracker(reseller_id);

-- =====================================================
-- 4. Add merchant_identifier to lead_call_tracker
-- =====================================================
ALTER TABLE lead_call_tracker
ADD COLUMN IF NOT EXISTS merchant_identifier VARCHAR(255);

UPDATE lead_call_tracker
SET merchant_identifier = shop_identifier
WHERE merchant_identifier IS NULL AND shop_identifier IS NOT NULL;

-- Individual index (analytics uses COALESCE with OR, not AND)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_merchant_identifier
    ON lead_call_tracker(merchant_identifier);

-- Composite index for queries filtering by (reseller_id, merchant_identifier)
-- Matches original (merchant_id, shop_identifier) composite index pattern
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_reseller_merchant_identifier
    ON lead_call_tracker(reseller_id, merchant_identifier);

-- =====================================================
-- 5. Add reseller_id to outbound_number
-- =====================================================
ALTER TABLE outbound_number
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE outbound_number
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- =====================================================
-- 6. Add merchant_identifier to outbound_number
-- =====================================================
ALTER TABLE outbound_number
ADD COLUMN IF NOT EXISTS merchant_identifier VARCHAR(255);

UPDATE outbound_number
SET merchant_identifier = shop_identifier
WHERE merchant_identifier IS NULL AND shop_identifier IS NOT NULL;

-- Composite index for queries filtering by (reseller_id, merchant_identifier)
-- Matches original (merchant_id, shop_identifier) composite index
CREATE INDEX IF NOT EXISTS idx_outbound_number_reseller_merchant_identifier
    ON outbound_number(reseller_id, merchant_identifier);

-- =====================================================
-- 7. Add reseller_id to template
-- =====================================================
ALTER TABLE template
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE template
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- =====================================================
-- 8. Add merchant_identifier to template
-- =====================================================
ALTER TABLE template
ADD COLUMN IF NOT EXISTS merchant_identifier VARCHAR(255);

UPDATE template
SET merchant_identifier = shop_identifier
WHERE merchant_identifier IS NULL AND shop_identifier IS NOT NULL;

-- Composite unique constraints on (reseller_id, merchant_identifier, name)
-- Matches original (merchant_id, shop_identifier, name) unique constraints

-- Unique constraint for shop-specific templates (when merchant_identifier IS NOT NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_template_reseller_merchant_identifier_name_not_null
    ON template(reseller_id, merchant_identifier, name)
    WHERE merchant_identifier IS NOT NULL;

-- Unique constraint for generic templates (when merchant_identifier IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_template_reseller_name_is_null
    ON template(reseller_id, name)
    WHERE merchant_identifier IS NULL;

-- Composite index for queries filtering by (reseller_id, merchant_identifier, name)
-- Note: Unique constraints above already provide index capability for these columns
CREATE INDEX IF NOT EXISTS idx_template_reseller_merchant_identifier_name
    ON template(reseller_id, merchant_identifier, name);

-- =====================================================
-- 9. Add reseller_ids to users
-- =====================================================
ALTER TABLE users
ADD COLUMN IF NOT EXISTS reseller_ids TEXT[];

UPDATE users
SET reseller_ids = merchant_ids
WHERE reseller_ids IS NULL AND merchant_ids IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_reseller_ids
    ON users USING GIN(reseller_ids);

-- =====================================================
-- 10. Add merchant_identifiers to users
-- =====================================================
ALTER TABLE users
ADD COLUMN IF NOT EXISTS merchant_identifiers TEXT[];

UPDATE users
SET merchant_identifiers = shop_identifiers
WHERE merchant_identifiers IS NULL AND shop_identifiers IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_merchant_identifiers
    ON users USING GIN(merchant_identifiers);

-- =====================================================
-- 11. Add reseller_id to credentials
-- =====================================================
ALTER TABLE credentials
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE credentials
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- Index for reseller-only queries
CREATE INDEX IF NOT EXISTS idx_credentials_reseller_id
    ON credentials(reseller_id);


-- =====================================================
-- 12. Add reseller_id to blacklisted_numbers
-- =====================================================
ALTER TABLE blacklisted_numbers
ADD COLUMN IF NOT EXISTS reseller_id VARCHAR(255);

UPDATE blacklisted_numbers
SET reseller_id = merchant_id
WHERE reseller_id IS NULL AND merchant_id IS NOT NULL;

-- Index for reseller-only queries
CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_reseller_id
    ON blacklisted_numbers(reseller_id);
