-- Migration: Template lineage (versioning) + family grouping for bulk operations
-- Description:
--   1. template: add current_version (head pointer) and family_id (bulk-op grouping)
--   2. template_bulk_op: one row per bulk update / bulk rollback (scope, patch, status)
--   3. template_version: append-only full snapshot of every template state.
--      History is never mutated; rollback appends a new version whose content
--      equals an older snapshot.
--   4. Backfill: current state of every existing template becomes version 1.

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS current_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS family_id UUID;

CREATE INDEX IF NOT EXISTS idx_template_family_id
    ON template(family_id)
    WHERE family_id IS NOT NULL;

-- Family is a first-class entity: named group that CONTAINS its base
-- (parent) template inline — same content columns as `template`.
-- Deliberately omitted from the copy: reseller_id/merchant_id (families are
-- platform-wide / admin-managed, not reseller-scoped — individual member
-- templates retain their own reseller_id),
-- telephony_number_id/is_active (the parent is not in the template table,
-- so it structurally cannot take calls or leads), and secrets (children
-- hold their own; duplicating them here is risk with no benefit).
CREATE TABLE IF NOT EXISTS template_family (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR NOT NULL,
    description TEXT,
    -- embedded parent template content (mirrors template's content columns)
    flow JSONB NOT NULL DEFAULT '{}',
    expected_payload_schema JSONB,
    expected_callback_response_schema JSONB,
    configurations JSONB,
    supported_channels TEXT[] NOT NULL DEFAULT ARRAY['voice']::text[],
    -- bumped on every parent-content edit; children record what they derive from
    base_version INTEGER NOT NULL DEFAULT 1,
    created_by VARCHAR(255),
    updated_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- family_id becomes a real FK now that the table exists. SET NULL keeps
-- member templates alive if a family is ever deleted.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_template_family_id'
          AND conrelid = 'template'::regclass
    ) THEN
        ALTER TABLE template
            ADD CONSTRAINT fk_template_family_id
            FOREIGN KEY (family_id) REFERENCES template_family(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS template_bulk_op (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    op_type VARCHAR(20) NOT NULL CHECK (op_type IN ('bulk_update', 'bulk_rollback')),
    family_id UUID,
    template_ids UUID[] NOT NULL,
    -- {"flow_patch": ..., "node_patches": ..., "configurations_patch": ...}
    patch JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed', 'rolled_back')),
    -- set on a bulk_rollback op: which bulk_update op it reverted
    reverted_bulk_op_id UUID REFERENCES template_bulk_op(id),
    initiated_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_template_bulk_op_family_id
    ON template_bulk_op(family_id);

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
        change_source IN ('backfill', 'create', 'manual_edit',
                          'bulk_update', 'rollback', 'bulk_rollback')
    ),
    bulk_op_id UUID REFERENCES template_bulk_op(id),
    changed_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_template_version UNIQUE (template_id, version)
);

CREATE INDEX IF NOT EXISTS idx_template_version_template_id
    ON template_version(template_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_template_version_bulk_op_id
    ON template_version(bulk_op_id)
    WHERE bulk_op_id IS NOT NULL;

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

-- ---------------------------------------------------------------------------
-- PHASE 2 (family propagation, docs/TEMPLATE_LINEAGE.md §6)
-- Every statement below is idempotent: this file may already have been
-- applied from this branch before Phase 2 was appended.
-- ---------------------------------------------------------------------------

-- Append-only snapshot of the family's parent-template content per
-- base_version. The three-way merge needs the OLD parent content as its
-- merge base; this also gives the parent a viewable history, same shape as
-- template_version. Content is stored verbatim from template_family, which
-- is mask-level by construction (no secrets column, configurations never
-- written with reveal_secrets).
CREATE TABLE IF NOT EXISTS template_family_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id UUID NOT NULL REFERENCES template_family(id) ON DELETE CASCADE,
    base_version INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    description TEXT,
    flow JSONB NOT NULL,
    expected_payload_schema JSONB,
    expected_callback_response_schema JSONB,
    configurations JSONB,
    supported_channels TEXT[] NOT NULL,
    changed_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_template_family_version UNIQUE (family_id, base_version)
);

CREATE INDEX IF NOT EXISTS idx_template_family_version_family_id
    ON template_family_version(family_id, base_version DESC);

-- Which family revision each child was last synced to. NULL = never
-- propagated to (the merge falls back to the previous family version; see
-- Task 14's NULL-sync rule). Deliberately NOT backfilled: existing members
-- have never been through a propagation, and claiming otherwise would make
-- the first merge treat their own content as "already synced".
ALTER TABLE template
    ADD COLUMN IF NOT EXISTS derived_from_base_version INTEGER;

-- Ledger: which family revision a propagation moved children FROM / TO.
-- from_base_version is what `POST /templates/bulk/rollback` restores the
-- family to when also_revert_family=true, and what children's
-- derived_from_base_version is reset to on revert.
ALTER TABLE template_bulk_op
    ADD COLUMN IF NOT EXISTS from_base_version INTEGER;

ALTER TABLE template_bulk_op
    ADD COLUMN IF NOT EXISTS to_base_version INTEGER;

-- op_type gains 'propagation'. A CHECK constraint cannot be altered in
-- place, so drop-and-re-add inside a guarded block. Two names are dropped:
-- the auto-generated one Postgres assigns to the inline CHECK in the
-- CREATE TABLE above (fresh apply), and the explicit one added here
-- (re-run of this file). Both DROPs are IF EXISTS, so the block is
-- idempotent and safe on a database that has never seen 046.
DO $$
BEGIN
    ALTER TABLE template_bulk_op
        DROP CONSTRAINT IF EXISTS template_bulk_op_op_type_check;
    ALTER TABLE template_bulk_op
        DROP CONSTRAINT IF EXISTS chk_template_bulk_op_op_type;
    ALTER TABLE template_bulk_op
        ADD CONSTRAINT chk_template_bulk_op_op_type
        CHECK (op_type IN ('bulk_update', 'bulk_rollback', 'propagation'));
END $$;

-- Backfill: every existing family's current content becomes the snapshot
-- for its current base_version, so a child that derives from it has a
-- merge base from day one.
INSERT INTO template_family_version (
    family_id, base_version, name, description, flow,
    expected_payload_schema, expected_callback_response_schema,
    configurations, supported_channels, changed_by
)
SELECT id, base_version, name, description, flow,
       expected_payload_schema, expected_callback_response_schema,
       configurations, supported_channels, COALESCE(updated_by, created_by)
FROM template_family
ON CONFLICT (family_id, base_version) DO NOTHING;
