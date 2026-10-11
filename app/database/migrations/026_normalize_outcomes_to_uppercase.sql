-- Migration 026: Normalize lead_call_tracker.outcome values to UPPERCASE.
-- All Clairvoyance code uses UPPERCASE outcome literals; this backfills any
-- historical rows where a bypass path may have written lowercase.
-- Going forward, the DB query layer enforces UPPER() at INSERT/UPDATE time.

BEGIN;

UPDATE lead_call_tracker
SET outcome = UPPER(outcome)
WHERE outcome IS NOT NULL
  AND outcome <> UPPER(outcome);

COMMIT;
