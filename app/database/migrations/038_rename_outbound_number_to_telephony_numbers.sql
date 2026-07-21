-- Migration 038: Rename outbound_number to telephony_numbers
-- Description: The table has stored inbound DIDs as well as outbound caller
--   IDs ever since inbound calling landed (template.outbound_number_id doubles
--   as the inbound routing key), so the old name is a misnomer. Renames the
--   table, its indexes and constraints, and leaves an updatable compatibility
--   VIEW named outbound_number so pods still running the previous release keep
--   working during a rolling deploy (a plain single-table view is
--   auto-updatable in Postgres, so old readers AND writers pass through).
-- Background: per-merchant number provisioning lands on top of this rename;
--   the reseller_id / merchant_id columns (added in 018, reshaped in 022)
--   become real ownership markers instead of vestigial metadata.
-- Note: column names (template.outbound_number_id,
--   lead_call_tracker.outbound_number_id) are NOT renamed — they are
--   wire-visible API fields and stay for client compatibility.
-- Follow-up: drop the outbound_number view in a later migration once no
--   pre-rename pods remain.

BEGIN;

ALTER TABLE IF EXISTS outbound_number RENAME TO telephony_numbers;

-- keep index names in step with the table (renames are metadata-only)
ALTER INDEX IF EXISTS idx_outbound_numbers_status RENAME TO idx_telephony_numbers_status;
ALTER INDEX IF EXISTS idx_outbound_numbers_provider RENAME TO idx_telephony_numbers_provider;
ALTER INDEX IF EXISTS idx_outbound_number_reseller_merchant_identifier RENAME TO idx_telephony_numbers_reseller_merchant;

-- constraint renames have no IF EXISTS form — guard by catalog lookup
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'outbound_number_pkey') THEN
        ALTER TABLE telephony_numbers RENAME CONSTRAINT outbound_number_pkey TO telephony_numbers_pkey;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'outbound_number_provider_check') THEN
        ALTER TABLE telephony_numbers RENAME CONSTRAINT outbound_number_provider_check TO telephony_numbers_provider_check;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'outbound_number_status_check') THEN
        ALTER TABLE telephony_numbers RENAME CONSTRAINT outbound_number_status_check TO telephony_numbers_status_check;
    END IF;
END $$;

-- rolling-deploy shim: pods on the previous release keep reading and writing
-- through the old name until they are cycled out
CREATE VIEW outbound_number AS SELECT * FROM telephony_numbers;

COMMIT;
