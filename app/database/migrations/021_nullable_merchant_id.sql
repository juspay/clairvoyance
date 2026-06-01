-- Migration: Make merchant_id and shop_identifier nullable
-- Description: Allow NULL values for the legacy columns merchant_id and
--   shop_identifier in tables where they are currently NOT NULL.


update users set reseller_ids = merchant_ids;

update users set merchant_identifiers = shop_identifiers;


-- call_execution_config
ALTER TABLE call_execution_config ALTER COLUMN merchant_id DROP NOT NULL;

-- lead_call_tracker
ALTER TABLE lead_call_tracker ALTER COLUMN merchant_id DROP NOT NULL;

-- template
ALTER TABLE template ALTER COLUMN merchant_id DROP NOT NULL;

-- users
ALTER TABLE users ALTER COLUMN merchant_ids DROP NOT NULL;