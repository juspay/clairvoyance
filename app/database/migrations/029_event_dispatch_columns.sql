-- Migration: event-driven dispatch observability column.
--
-- Nullable so no backfill is needed; old rows keep NULL.
-- See docs/BACKLOG_DISPATCHER_REDESIGN.md.
--
--   dispatched_at  Set by the dispatch worker when the lead is popped off
--                  the ready list and dispatch begins. Pairs with
--                  ``call_initiated_time`` (set after provider.make_call
--                  returns) to measure event-path latency end-to-end:
--                  ZADD time -> dispatched_at -> call_initiated_time.

ALTER TABLE "lead_call_tracker"
    ADD COLUMN IF NOT EXISTS "dispatched_at" TIMESTAMP WITH TIME ZONE NULL;

-- No CHECK constraint changes. No enum additions. No index changes.
