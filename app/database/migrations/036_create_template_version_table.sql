-- Template version history (design: docs/TEMPLATE_VERSIONING.md).
--
-- Append-only snapshots of a template's versioned content (flow +
-- configurations + payload schemas), written in the SAME transaction as
-- every template create/update/rollback. The active version is always
-- MAX(version_number); the live `template` row always equals it. Rollback
-- appends a new row (change_source='rollback', restored_from=n) — nothing
-- is ever deleted or modified here.
--
-- Secrets: template.secrets is never snapshotted. MCP auth values inside
-- configurations are stored masked ('**********'); on rollback the live
-- row's real values are carried forward via merge_masked_mcp_auth.
--
-- Retention: keep only the 10 most recent versions per template, enforced
-- in the database (via an AFTER INSERT trigger) rather than the
-- application, so that EVERY insert path is covered identically -- API
-- save, rollback, backfill, or a manual INSERT -- and so pruning is atomic
-- with the insert that triggers it: if the save rolls back, the prune
-- rolls back with it.
--
-- Numbering is never reused. A template that reaches v11 keeps v2..v11; the
-- ordinals stay stable forever and simply develop a gap at the bottom.
--
-- To change the limit later, ship a new migration that CREATE OR REPLACEs the
-- function with a different OFFSET (the trigger itself does not need touching).

CREATE TABLE IF NOT EXISTS template_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    flow JSONB NOT NULL,
    configurations JSONB,
    expected_payload_schema JSONB,
    expected_callback_response_schema JSONB,
    updated_by VARCHAR(255),
    change_source VARCHAR(16) NOT NULL
        CHECK (change_source IN ('create', 'update', 'rollback')),
    restored_from INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (template_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_template_version_template_id
    ON template_version (template_id, version_number DESC);

CREATE OR REPLACE FUNCTION prune_template_versions()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- Rank-based rather than arithmetic (`version_number <= NEW.version_number - 10`)
    -- so the retention count stays exactly right even when history already has
    -- gaps from earlier pruning or manual deletes.
    DELETE FROM template_version
    WHERE id IN (
        SELECT id
        FROM template_version
        WHERE template_id = NEW.template_id
        ORDER BY version_number DESC
        OFFSET 10
    );
    RETURN NULL;  -- AFTER trigger: return value is ignored
END;
$$;

DROP TRIGGER IF EXISTS trg_prune_template_versions ON template_version;

CREATE TRIGGER trg_prune_template_versions
AFTER INSERT ON template_version
FOR EACH ROW
EXECUTE FUNCTION prune_template_versions();

-- Backfill: give every existing template a version-1 baseline from its live
-- row so history/diff/rollback work from day one. MCP auth secret fields
-- (configurations->mcp->servers[*]->auth->{token,password,api_key_value})
-- are masked to '**********' — same mask HttpAuthConfig's field_serializer
-- emits — so no credential material lands in history. ON CONFLICT makes the
-- backfill idempotent if the migration ever re-runs.
INSERT INTO template_version (
    template_id, version_number, name, flow, configurations,
    expected_payload_schema, expected_callback_response_schema,
    updated_by, change_source, restored_from, created_at
)
SELECT
    t.id,
    1,
    t.name,
    t.flow,
    CASE
        WHEN t.configurations IS NULL THEN NULL
        WHEN jsonb_typeof(t.configurations #> '{mcp,servers}') IS DISTINCT FROM 'array'
            THEN t.configurations
        ELSE jsonb_set(
            t.configurations,
            '{mcp,servers}',
            (
                SELECT COALESCE(
                    jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(s -> 'auth') = 'object' THEN
                                jsonb_set(
                                    s,
                                    '{auth}',
                                    (s -> 'auth')
                                    || CASE WHEN (s -> 'auth') ? 'token'
                                        THEN '{"token": "**********"}'::jsonb
                                        ELSE '{}'::jsonb END
                                    || CASE WHEN (s -> 'auth') ? 'password'
                                        THEN '{"password": "**********"}'::jsonb
                                        ELSE '{}'::jsonb END
                                    || CASE WHEN (s -> 'auth') ? 'api_key_value'
                                        THEN '{"api_key_value": "**********"}'::jsonb
                                        ELSE '{}'::jsonb END
                                )
                            ELSE s
                        END
                        ORDER BY ord
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(t.configurations #> '{mcp,servers}')
                    WITH ORDINALITY AS e(s, ord)
            )
        )
    END,
    t.expected_payload_schema,
    t.expected_callback_response_schema,
    NULL,
    'create',
    NULL,
    t.updated_at
FROM template t
ON CONFLICT (template_id, version_number) DO NOTHING;

-- One-time cleanup for any template that already exceeds the limit (no-op on a
-- fresh install; the trigger only fires on inserts made after it exists).
DELETE FROM template_version
WHERE id IN (
    SELECT id
    FROM (
        SELECT id,
               row_number() OVER (PARTITION BY template_id ORDER BY version_number DESC) AS rn
        FROM template_version
    ) ranked
    WHERE rn > 10
);
