-- 071: merchant-scoped credentials.
--
-- A credential row was reseller-wide (reseller_id) or global (NULL). Some
-- integrations are per merchant — the agentic "uap" row holds one Juspay
-- merchant and one NammaYatri host — so a row may now name a merchant too.
-- Resolution is most-specific-wins: merchant row, else the reseller row,
-- else the global row. Existing rows have merchant_id NULL and behave
-- exactly as before.
ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS merchant_id varchar NULL;

-- A merchant row always sits under its reseller (tenancy law): no
-- merchant-scoped global credentials.
ALTER TABLE credentials
    DROP CONSTRAINT IF EXISTS credentials_merchant_needs_reseller;
ALTER TABLE credentials
    ADD CONSTRAINT credentials_merchant_needs_reseller
    CHECK (merchant_id IS NULL OR reseller_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS credentials_scope_ix
    ON credentials (reseller_id, merchant_id, name)
    WHERE is_active;
