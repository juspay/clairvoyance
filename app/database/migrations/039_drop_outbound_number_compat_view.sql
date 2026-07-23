-- Migration 039: Drop the outbound_number compatibility view
-- Description: Migration 038 renamed outbound_number -> telephony_numbers and
--   left an updatable VIEW under the old name so pods still running the
--   pre-rename release kept working during the rolling deploy. That deploy
--   completed on 2026-07-22 and every pod has been on post-rename code since;
--   the view's rollback-insurance window is over.
-- Safety: verified no live code references the bare outbound_number relation —
--   the only matches in the repo are historical migration files (never
--   re-run) and Plivo request payload keys (dict keys, not SQL).
-- Note: this intentionally does NOT touch the wire-visible column names
--   (template.outbound_number_id, lead_call_tracker.outbound_number_id) —
--   those retire via the dual-field API window, not a DB migration.

DROP VIEW IF EXISTS outbound_number;
