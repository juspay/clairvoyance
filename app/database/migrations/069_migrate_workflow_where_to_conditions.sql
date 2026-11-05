-- 069: entry.where graduates from an equality map to typed conditions
-- (design/event-catalog.md §The where-grammar; companion ruling 1 Sep 2026:
-- "legacy entry.where equality maps -> ONE migration to the typed condition
-- list (definition + draft), validator accepts lists only").
--
--   {"gateway": "COD"}  ->  [{"field": "payload.gateway", "op": "is", "value": "COD"}]
--
-- Data-only; touches crm_workflow (outreach) and nothing else. Idempotent:
-- only documents (or doors) whose where is still an object are rewritten,
-- so a re-run is a no-op. Both entry shapes: the single object and the
-- list of doors (phase 15). The touch trigger stamps updated_at; version
-- is NOT bumped (no publish happened — the plan's meaning is unchanged).

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

-- The list shape (rollout phase 15): a plan's entry may be a LIST of doors,
-- each with its own where. The object-shape UPDATEs above see NULL at
-- entry->where for those, so every door is rebuilt here — only the doors
-- still holding a map change, order kept, and a list with no map is not
-- touched at all (idempotent, same as above).

UPDATE crm_workflow
SET definition = jsonb_set(
    definition,
    '{entry}',
    (SELECT jsonb_agg(
                CASE WHEN jsonb_typeof(d -> 'where') = 'object'
                     THEN jsonb_set(d, '{where}', COALESCE(
                              (SELECT jsonb_agg(jsonb_build_object(
                                          'field', 'payload.' || e.key,
                                          'op', 'is', 'value', e.value))
                               FROM jsonb_each(d -> 'where') AS e),
                              '[]'::jsonb))
                     ELSE d
                END ORDER BY ord)
     FROM jsonb_array_elements(definition -> 'entry') WITH ORDINALITY AS t(d, ord))
)
WHERE jsonb_typeof(definition -> 'entry') = 'array'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(definition -> 'entry') AS d
              WHERE jsonb_typeof(d -> 'where') = 'object');

UPDATE crm_workflow
SET draft = jsonb_set(
    draft,
    '{entry}',
    (SELECT jsonb_agg(
                CASE WHEN jsonb_typeof(d -> 'where') = 'object'
                     THEN jsonb_set(d, '{where}', COALESCE(
                              (SELECT jsonb_agg(jsonb_build_object(
                                          'field', 'payload.' || e.key,
                                          'op', 'is', 'value', e.value))
                               FROM jsonb_each(d -> 'where') AS e),
                              '[]'::jsonb))
                     ELSE d
                END ORDER BY ord)
     FROM jsonb_array_elements(draft -> 'entry') WITH ORDINALITY AS t(d, ord))
)
WHERE jsonb_typeof(draft -> 'entry') = 'array'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(draft -> 'entry') AS d
              WHERE jsonb_typeof(d -> 'where') = 'object');
