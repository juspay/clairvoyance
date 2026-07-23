-- Migration 040: Rename outbound_number_id columns to telephony_number_id
-- Description: Completes the telephony rename started in 038 (table) and 039
--   (compat view drop). The template pin and the lead's dialed-number column
--   both stored "outbound_number_id", a misnomer since the same id routes
--   inbound calls. Renames the columns and their indexes on template and
--   lead_call_tracker. No FK constraints reference these columns (verified
--   against the catalog), so the two column renames + two index renames are
--   the whole change. Metadata-only; no data movement, no locks beyond the
--   brief ACCESS EXCLUSIVE each ALTER takes.
-- Deploy note: application code names these columns explicitly in SQL, so
--   run this migration and roll pods in the same release window (same
--   runbook as 038): pods on pre-rename code error on template/lead reads
--   once this applies. Keep the migrate -> deploy gap to minutes.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'template' AND column_name = 'outbound_number_id'
    ) THEN
        ALTER TABLE template RENAME COLUMN outbound_number_id TO telephony_number_id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'lead_call_tracker' AND column_name = 'outbound_number_id'
    ) THEN
        ALTER TABLE lead_call_tracker RENAME COLUMN outbound_number_id TO telephony_number_id;
    END IF;
END $$;

ALTER INDEX IF EXISTS idx_template_outbound_number_id
    RENAME TO idx_template_telephony_number_id;
ALTER INDEX IF EXISTS idx_lead_call_tracker_outbound_number_id
    RENAME TO idx_lead_call_tracker_telephony_number_id;

COMMIT;
