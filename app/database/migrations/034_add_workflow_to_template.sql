-- Migration 034: Add workflow classifier to template (+ backfill, indexes)

BEGIN;

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS workflow VARCHAR(50) NOT NULL DEFAULT 'non-shopify';

UPDATE template
SET workflow = CASE
    WHEN name ILIKE '%assist%' THEN 'assist'
    WHEN name ILIKE '%abandon%' OR name ILIKE '%recovery%' THEN 'abandonment-recovery'
    WHEN name ILIKE '%confirmation%' THEN 'order-confirmation'
    ELSE 'test'
END
WHERE reseller_id = 'BB_SHOPIFY';

CREATE INDEX IF NOT EXISTS idx_template_workflow
    ON template(workflow);

CREATE INDEX IF NOT EXISTS idx_template_workflow_reseller
    ON template(workflow, reseller_id);

COMMIT;
