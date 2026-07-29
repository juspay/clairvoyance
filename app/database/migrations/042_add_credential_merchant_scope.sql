-- Migration 042: Add merchant scope to credentials.
--
-- Scope combinations:
--   NULL reseller + NULL merchant: global
--   reseller + NULL merchant: reseller-shared
--   reseller + merchant: merchant-specific

ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_credentials_reseller_merchant_active
    ON credentials(reseller_id, merchant_id)
    WHERE is_active = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_global_name
    ON credentials(name)
    WHERE reseller_id IS NULL
      AND merchant_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_reseller_shared_name
    ON credentials(reseller_id, name)
    WHERE reseller_id IS NOT NULL
      AND merchant_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_merchant_name
    ON credentials(reseller_id, merchant_id, name)
    WHERE reseller_id IS NOT NULL
      AND merchant_id IS NOT NULL;
