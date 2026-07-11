-- Migration: Link call_execution_config rows to their owning template
-- Description:
--   1. Backfill template_id on configs whose owning template is unambiguous
--      (exact reseller + merchant + name match; NULL-safe on merchant so
--      reseller-level rows link to their reseller-level template).
--   2. Enforce one config per template with a partial unique index.
--
-- Background:
--   template_id (added in migration 008, FK -> template.id) is the ONLY
--   runtime linkage between a template and its calling config — resolution
--   is strictly by id, there is no name-based lookup or fallback anywhere.
--   Rows created before the API set template_id are name-linked only; this
--   migration adopts them. The name join below is the one-time conversion
--   of legacy name links into id links, not runtime logic.

UPDATE call_execution_config c
SET template_id = t.id,
    updated_at  = NOW()
FROM template t
WHERE c.template_id IS NULL
  AND t.reseller_id = c.reseller_id
  AND t.merchant_id IS NOT DISTINCT FROM c.merchant_id
  AND t.name = c.template
  -- skip configs whose name matches more than one template in scope
  AND NOT EXISTS (
        SELECT 1 FROM template t2
        WHERE t2.reseller_id = c.reseller_id
          AND t2.merchant_id IS NOT DISTINCT FROM c.merchant_id
          AND t2.name = c.template
          AND t2.id <> t.id
  )
  -- one config per template: never create a second link
  AND NOT EXISTS (
        SELECT 1 FROM call_execution_config c2
        WHERE c2.template_id = t.id
          AND c2.id <> c.id
  )
  -- don't link a merchant config while in-flight leads are pinned to a
  -- different template (their config match would break until they drain)
  AND NOT EXISTS (
        SELECT 1 FROM lead_call_tracker l
        WHERE l.merchant_id = c.merchant_id
          AND l.template = c.template
          AND l.status IN ('BACKLOG', 'RETRY', 'PROCESSING')
          AND l.template_id IS DISTINCT FROM t.id
  );

-- One config per template (unlinked legacy rows exempt during transition).
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_template_id
    ON call_execution_config (template_id)
    WHERE template_id IS NOT NULL;
