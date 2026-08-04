-- 035_create_alert_groups_table.sql
-- Alert groups: named sets of phone numbers to call when an alert fires.
-- Scoped by reseller_id so each reseller manages its own groups.
-- members is a JSONB array: [{"name": "Alice", "phone": "+919876543210"}, ...]

CREATE TABLE IF NOT EXISTS alert_groups (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    reseller_id  TEXT        NOT NULL,
    members      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, reseller_id)
);

CREATE INDEX IF NOT EXISTS idx_alert_groups_reseller_id ON alert_groups (reseller_id);
