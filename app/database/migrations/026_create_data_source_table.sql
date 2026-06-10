-- Migration 026: Create data_source table for external data source definitions
-- A data source defines how to fetch external data (e.g., a Google Sheet tab)
-- that can be injected into template variables or LLM context at call time.

CREATE TABLE IF NOT EXISTS data_source (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id     VARCHAR(255) NOT NULL,
    merchant_id     VARCHAR(255),                   -- NULL = reseller-level (shared across merchants)
    name            VARCHAR(255) NOT NULL,           -- user-given name; becomes {name} placeholder
    source_type     VARCHAR(50)  NOT NULL DEFAULT 'google_sheet'
                        CHECK (source_type IN ('google_sheet')),
    spreadsheet_url TEXT         NOT NULL,           -- full URL pasted by user
    spreadsheet_id  VARCHAR(255) NOT NULL,           -- extracted from URL
    sheet_name      VARCHAR(255),                   -- tab name; NULL = first tab
    columns         JSONB,                          -- ["Col1","Col2"]; NULL = all columns
    format          VARCHAR(50)  NOT NULL DEFAULT 'markdown_table'
                        CHECK (format IN ('markdown_table', 'csv', 'json')),
    is_active       BOOLEAN      NOT NULL DEFAULT true,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Scoped lookup indexes
CREATE INDEX IF NOT EXISTS idx_data_source_reseller_id ON data_source(reseller_id);
CREATE INDEX IF NOT EXISTS idx_data_source_merchant_id ON data_source(merchant_id);
CREATE INDEX IF NOT EXISTS idx_data_source_is_active   ON data_source(is_active);

-- Unique name within reseller+merchant scope
CREATE UNIQUE INDEX IF NOT EXISTS uq_data_source_reseller_merchant_name
    ON data_source(reseller_id, merchant_id, name)
    WHERE merchant_id IS NOT NULL;

-- Unique name at reseller level (no merchant)
CREATE UNIQUE INDEX IF NOT EXISTS uq_data_source_reseller_name_null_merchant
    ON data_source(reseller_id, name)
    WHERE merchant_id IS NULL;
