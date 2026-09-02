-- 069: entry.where graduates from an equality map to typed conditions
-- (design/event-catalog.md §The where-grammar; companion ruling 1 Sep 2026:
-- "legacy entry.where equality maps -> ONE migration to the typed condition
-- list (definition + draft), validator accepts lists only").
--
--   {"gateway": "COD"}  ->  [{"field": "payload.gateway", "op": "is", "value": "COD"}]
--
-- Data-only; touches crm_workflow (outreach) and nothing else. Idempotent:
-- only documents whose entry.where is still an object are rewritten, so a
-- re-run is a no-op. The touch trigger stamps updated_at; version is NOT
-- bumped (no publish happened — the plan's meaning is unchanged).

UPDATE crm_workflow
SET definition = jsonb_set(
    definition,
    '{entry,where}',
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object(
                    'field', 'payload.' || e.key, 'op', 'is', 'value', e.value))
         FROM jsonb_each(definition -> 'entry' -> 'where') AS e),
        '[]'::jsonb)
)
WHERE jsonb_typeof(definition -> 'entry' -> 'where') = 'object';

UPDATE crm_workflow
SET draft = jsonb_set(
    draft,
    '{entry,where}',
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object(
                    'field', 'payload.' || e.key, 'op', 'is', 'value', e.value))
         FROM jsonb_each(draft -> 'entry' -> 'where') AS e),
        '[]'::jsonb)
)
WHERE jsonb_typeof(draft -> 'entry' -> 'where') = 'object';
