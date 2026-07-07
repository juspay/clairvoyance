-- Migration 035: Create reusable Breeze Buddy data-source records.
--
-- Data sources are standalone, reusable external datasets that templates can
-- attach by reference. The source-specific connection details live in
-- JSONB so v1 can ship Google Sheets while preserving a stable table shape for
-- CSV/Excel/docs/DB snapshots later.

CREATE TABLE IF NOT EXISTS data_source (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id     text NOT NULL,
    merchant_id     text,
    name            text NOT NULL,
    source_type     text NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active       boolean NOT NULL DEFAULT TRUE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_data_source_reseller_merchant
    ON data_source (reseller_id, merchant_id);

CREATE INDEX IF NOT EXISTS idx_data_source_source_type
    ON data_source (source_type);

CREATE UNIQUE INDEX IF NOT EXISTS idx_data_source_unique_active_name
    ON data_source (reseller_id, COALESCE(merchant_id, ''), name)
    WHERE is_active = TRUE;
