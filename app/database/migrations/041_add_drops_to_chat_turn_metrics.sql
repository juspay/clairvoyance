-- Migration 041: Persist per-drop evidence on chat_turn_metrics.
--
-- 032 stores per-turn *counts* (ui_dropped) — enough to see THAT ops were
-- dropped, never WHAT. Diagnosing the real-traffic mailto: Button class took
-- log archaeology plus transcript inference to reconstruct the dropped op.
-- This column keeps the evidence next to the count: a jsonb array of
--
--   {"sig":    {"op":"add","id":"email_btn","type":"Button"},
--    "reason": "props_validation_failed:Button:action.OpenUrlAction.url:url_scheme",
--    "raw":    "<the dropped JSONL line, capped>"}
--
-- Deliberate amendment to 032's "no payload content" note: ``raw`` is
-- assistant-GENERATED op content for this same session — the identical
-- sensitivity class as chat_message.content one join away (same session FK,
-- same cascade delete, same read path). It never contains user-typed text
-- the transcript doesn't already hold. Logs stay structural-only; the raw
-- evidence lives ONLY here, under transcript retention/access rules.
--
-- NULL (not '[]') when a turn dropped nothing — keeps the common row narrow.

ALTER TABLE chat_turn_metrics
    ADD COLUMN IF NOT EXISTS drops jsonb;
