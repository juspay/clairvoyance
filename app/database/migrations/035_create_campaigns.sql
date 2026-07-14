-- Campaigns: a named bulk lead push (console feature, 2026-07-14).
--
-- A campaign is the grouping + aggregate anchor for a batch of leads pushed
-- together from the console. Each lead created through POST /campaigns is a
-- perfectly ordinary lead_call_tracker row (same validation, dispatch and
-- retry machinery as POST /leads) additionally stamped with campaign_id.
--
-- Status lifecycle: RUNNING -> COMPLETED (lazily, when no BACKLOG/PROCESSING
-- leads remain) or RUNNING -> STOPPED (explicit stop aborts pending leads).

CREATE TABLE IF NOT EXISTS campaign (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255),
    template_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING', 'COMPLETED', 'STOPPED')),
    total_leads INTEGER NOT NULL DEFAULT 0,
    skipped_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_merchant_created
    ON campaign (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_campaign_reseller_created
    ON campaign (reseller_id, created_at DESC);

ALTER TABLE lead_call_tracker ADD COLUMN IF NOT EXISTS campaign_id UUID;

CREATE INDEX IF NOT EXISTS idx_lct_campaign_id
    ON lead_call_tracker (campaign_id) WHERE campaign_id IS NOT NULL;
