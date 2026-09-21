-- Migration: Template lineage (versioning)
-- Description:
--   1. template: add current_version (head pointer), bumped on every write
--   2. template_version: append-only full snapshot of every template state.
--      History is never mutated; rollback appends a new version whose content
--      equals an older snapshot.
--   3. Backfill: current state of every existing template becomes version 1.

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS current_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS template_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    -- Full snapshot of every editable template column at this version.
    reseller_id VARCHAR(255),
    merchant_id VARCHAR(255),
    name VARCHAR NOT NULL,
    flow JSONB NOT NULL,
    expected_payload_schema JSONB,
    expected_callback_response_schema JSONB,
    configurations JSONB,
    secrets JSONB,
    -- deliberately NO FK: history must survive later number deletion
    telephony_number_id UUID,
    is_active BOOLEAN,
    supported_channels TEXT[] NOT NULL,
    change_source VARCHAR(20) NOT NULL CHECK (
        change_source IN ('backfill', 'create', 'manual_edit', 'rollback')
    ),
    -- Which bulk operation wrote this snapshot. Always NULL until the bulk-op
    -- ledger exists (a later migration adds template_bulk_op, this column's
    -- FK and its index); the snapshot writer already records it so the two
    -- never have to diverge.
    bulk_op_id UUID,
    changed_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_template_version UNIQUE (template_id, version)
);

CREATE INDEX IF NOT EXISTS idx_template_version_template_id
    ON template_version(template_id, version DESC);

-- Backfill: snapshot the current state of every template as version 1.
INSERT INTO template_version (
    template_id, version, reseller_id, merchant_id, name, flow,
    expected_payload_schema, expected_callback_response_schema,
    configurations, secrets, telephony_number_id, is_active,
    supported_channels, change_source
)
SELECT id, 1, reseller_id, merchant_id, name, flow,
       expected_payload_schema, expected_callback_response_schema,
       configurations, secrets, telephony_number_id, is_active,
       supported_channels, 'backfill'
FROM template
ON CONFLICT (template_id, version) DO NOTHING;
