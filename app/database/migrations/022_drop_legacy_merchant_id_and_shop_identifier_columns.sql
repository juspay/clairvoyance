-- Migration 022: Drop legacy columns and rename merchant_identifier to merchant_id
--
-- Background:
--   Original columns were misnamed:
--     merchant_id      → was actually reseller (replaced by reseller_id)
--     shop_identifier  → was actually merchant (replaced by merchant_identifier)
--   Migration 018 added reseller_id + merchant_identifier alongside the old columns.
--   This migration:
--     1. Backfills any remaining NULLs
--     2. Drops the old misnamed columns (merchant_id, shop_identifier)
--     3. Renames merchant_identifier → merchant_id (correct name for the concept)
--
-- Final column state: reseller_id + merchant_id
-- WARNING: IRREVERSIBLE. Back up your database first.
-- Only run AFTER deploying code that uses reseller_id + merchant_id.

BEGIN;

-- =====================================================
-- 1. Backfill any remaining NULL reseller_id / merchant_identifier
-- =====================================================
UPDATE lead_call_tracker SET reseller_id = merchant_id;
UPDATE lead_call_tracker SET merchant_identifier = shop_identifier;

UPDATE call_execution_config SET reseller_id = merchant_id;
UPDATE call_execution_config SET merchant_identifier = shop_identifier;

UPDATE outbound_number SET reseller_id = merchant_id;
UPDATE outbound_number SET merchant_identifier = shop_identifier;

UPDATE template SET reseller_id = merchant_id;
UPDATE template SET merchant_identifier = shop_identifier;

UPDATE blacklisted_numbers SET reseller_id = merchant_id;

UPDATE credentials SET reseller_id = merchant_id;

-- =====================================================
-- 2. lead_call_tracker: drop old columns, rename merchant_identifier → merchant_id
-- =====================================================
ALTER TABLE lead_call_tracker DROP COLUMN IF EXISTS merchant_id;
ALTER TABLE lead_call_tracker DROP COLUMN IF EXISTS shop_identifier;
ALTER TABLE lead_call_tracker RENAME COLUMN merchant_identifier TO merchant_id;

-- =====================================================
-- 3. call_execution_config: drop old columns, rename merchant_identifier → merchant_id
-- =====================================================
ALTER TABLE call_execution_config DROP COLUMN IF EXISTS merchant_id;
ALTER TABLE call_execution_config DROP COLUMN IF EXISTS shop_identifier;
ALTER TABLE call_execution_config RENAME COLUMN merchant_identifier TO merchant_id;

-- =====================================================
-- 4. outbound_number: drop old columns, rename merchant_identifier → merchant_id
-- =====================================================
ALTER TABLE outbound_number DROP COLUMN IF EXISTS merchant_id;
ALTER TABLE outbound_number DROP COLUMN IF EXISTS shop_identifier;
ALTER TABLE outbound_number RENAME COLUMN merchant_identifier TO merchant_id;

-- =====================================================
-- 5. template: drop old columns, rename merchant_identifier → merchant_id
-- =====================================================
ALTER TABLE template DROP COLUMN IF EXISTS merchant_id;
ALTER TABLE template DROP COLUMN IF EXISTS shop_identifier;
ALTER TABLE template RENAME COLUMN merchant_identifier TO merchant_id;

-- =====================================================
-- 6. blacklisted_numbers: drop old column
--    (no merchant_identifier column exists here)
-- =====================================================
ALTER TABLE blacklisted_numbers DROP COLUMN IF EXISTS merchant_id;

-- =====================================================
-- 7. credentials: drop old merchant_id column
--    (only has reseller_id, no merchant_identifier/shop_identifier)
-- =====================================================
ALTER TABLE credentials DROP COLUMN IF EXISTS merchant_id;

-- =====================================================
-- 8. merchants: rename merchant_identifier → merchant_id
-- =====================================================
ALTER TABLE merchants RENAME COLUMN merchant_identifier TO merchant_id;

-- =====================================================
-- 9. users: rename merchant_identifiers → merchant_ids
-- =====================================================
ALTER TABLE users DROP COLUMN IF EXISTS merchant_ids;
ALTER TABLE users DROP COLUMN IF EXISTS shop_identifiers;
ALTER TABLE users RENAME COLUMN merchant_identifiers TO merchant_ids;

COMMIT;