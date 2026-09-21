-- Migration: count a number's calls holding lines, without a table scan
-- Description: the walker's room (outreach/capacity.py) asks, before a cold
-- run's call is written, how many calls already hold the number's lines —
-- queued, retrying or on the line, on every template that dials through
-- it — and writes the call only while that stays under the number's lines:
--
--   SELECT count(*) FROM lead_call_tracker
--   WHERE template_id = ANY($1) AND status IN ('BACKLOG', 'RETRY', 'PROCESSING')
--
-- lead_call_tracker holds every merchant's every lead, and a busy
-- template's FINISHED rows run to hundreds of thousands, so the existing
-- (template_id) index would walk a day of history for a count of a few
-- hundred. The partial index below holds exactly the live rows, so the
-- count costs an index range at any table size.
--
-- On prod: CREATE INDEX CONCURRENTLY first, then apply this file.

CREATE INDEX IF NOT EXISTS lead_call_tracker_queued_by_template_ix
    ON lead_call_tracker (template_id)
    WHERE status IN ('BACKLOG', 'RETRY', 'PROCESSING');
