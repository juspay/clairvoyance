-- Copy every template's observers into evaluation_config.
--
-- The runtime reads that row first and only falls back to the template JSON
-- when none exists — but a detection cannot be stored on the fallback path,
-- because evaluation_result.evaluation_config_id is a NOT NULL FK. Without this
-- backfill, a template's detections stay unrecorded until someone happens to
-- save it from the console.
--
-- Runs after 046 (adds the OBSERVER enum value) and 047 (widens the runtime
-- check to accept the { "observers": [...] } shape) — both are required for
-- these rows to insert at all.
--
-- ON CONFLICT DO NOTHING: rows the API has already written are the fresher
-- copy, so this must never overwrite them. That also makes the migration safe
-- to re-run.
INSERT INTO evaluation_config (
    template_id, evaluation_type, enabled, topics, configuration
)
SELECT
    t.id,
    'OBSERVER',
    TRUE,
    ARRAY[]::text[],
    jsonb_build_object('observers', t.configurations -> 'observers')
FROM template t
-- Compared as jsonb rather than via jsonb_array_length: some templates hold a
-- non-array under this key, and Postgres does not promise to evaluate the type
-- check first, so the length call errors on them.
WHERE jsonb_typeof(t.configurations -> 'observers') = 'array'
  AND t.configurations -> 'observers' <> '[]'::jsonb
ON CONFLICT (template_id, evaluation_type) DO NOTHING;
