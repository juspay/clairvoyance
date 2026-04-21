-- Migration 025: Add per-merchant pickup rate alert configuration
-- Extends merchants table with opt-in alerting columns.
-- NULL threshold means "use global PICKUP_RATE_ALERT_THRESHOLD dynamic config".

BEGIN;

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS pickup_rate_alert_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pickup_rate_alert_threshold FLOAT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_merchants_pickup_rate_alert_enabled
    ON merchants(pickup_rate_alert_enabled);

COMMIT;
