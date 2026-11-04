-- Migration 086: merchant-level blacklist entries and scope uniqueness
-- Description: blacklisted_numbers gains a third scope, and each phone can
-- hold only one entry per scope:
--   reseller_id NULL, merchant_id NULL    → global, blocks everyone
--   reseller_id set,  merchant_id NULL    → blocks all of that reseller's merchants
--   reseller_id set,  merchant_id set     → blocks that merchant only
--
-- Uniqueness has been missing since 022: the 017 unique indexes were built on
-- the old merchant_id column, and dropping that column dropped them too.
-- (This merchant_id is a new column. The old one held what is now reseller_id.)
--
-- PRODUCTION NOTE: duplicates may exist, and the unique index below fails on
-- them, rolling back this whole file. Clear them FIRST. This lists what is left:
--
--   SELECT phone_number, COALESCE(reseller_id, '') AS reseller, COUNT(*)
--   FROM blacklisted_numbers
--   GROUP BY 1, 2
--   HAVING COUNT(*) > 1;

ALTER TABLE blacklisted_numbers
ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(255);

-- A merchant entry without its reseller would read as a GLOBAL block.
ALTER TABLE blacklisted_numbers
ADD CONSTRAINT blacklisted_numbers_merchant_needs_reseller
CHECK (merchant_id IS NULL OR reseller_id IS NOT NULL);

-- COALESCE because NULLs never conflict in a plain unique index, which would
-- leave global and reseller-level entries free to repeat.
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone_scope
    ON blacklisted_numbers (
        phone_number,
        COALESCE(reseller_id, ''),
        COALESCE(merchant_id, '')
    );

COMMENT ON COLUMN blacklisted_numbers.merchant_id IS
    'Set to block the number for this merchant only. Requires reseller_id.';
