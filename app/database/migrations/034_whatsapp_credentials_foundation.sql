-- Migration 034: Merchant connector foundation
--
-- Adds merchant-scoped connector credentials, generic connector state, and
-- daily connector metrics. This migration is unapplied with this foundation
-- branch, so it intentionally defines the final generic schema directly.

BEGIN;

ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(255);

ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS template_exposable BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_credentials_reseller_merchant
    ON credentials(reseller_id, merchant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_active_connector_secret
    ON credentials(reseller_id, merchant_id, name)
    WHERE is_active = TRUE
      AND template_exposable = FALSE
      AND reseller_id IS NOT NULL
      AND merchant_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS connectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255) NOT NULL,
    connector VARCHAR(255) NOT NULL,
    credential_id UUID REFERENCES credentials(id) ON DELETE SET NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'connected'
        CHECK (status IN ('connected', 'disconnected', 'error')),
    connected_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    disconnected_at TIMESTAMP WITH TIME ZONE,
    last_sync_at TIMESTAMP WITH TIME ZONE,
    -- Static provider configuration only; counters belong in connector_metrics.
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_connectors_reseller_merchant_connector
        UNIQUE (reseller_id, merchant_id, connector)
);

CREATE INDEX IF NOT EXISTS idx_connectors_merchant_connector
    ON connectors (merchant_id, connector);

CREATE INDEX IF NOT EXISTS idx_connectors_credential_id
    ON connectors (credential_id);

CREATE TABLE IF NOT EXISTS connector_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id UUID NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    merchant_id VARCHAR(255) NOT NULL,
    reseller_id VARCHAR(255) NOT NULL,
    metric_date DATE NOT NULL,
    metric_name VARCHAR(255) NOT NULL,
    value BIGINT NOT NULL DEFAULT 0 CHECK (value >= 0),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_daily_connector_metric
        UNIQUE (connector_id, metric_date, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_connector_metrics_query
    ON connector_metrics (merchant_id, connector_id, metric_date);

COMMIT;
