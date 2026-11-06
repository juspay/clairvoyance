-- 078: merchants.call_limits (ADR 0025 stage 1, task V1) — the merchant's
-- per-customer call rule, enforced at the dial by the dispatch worker.
--
-- A LIST of {max_calls, window_hours} rules: at most `max_calls` dials to one
-- customer in any rolling `window_hours`. Stage 1 accepts exactly one entry;
-- it is a list so "2 a day and 5 a week" (stage 2) is a second entry, not a
-- migration. NULL means the merchant has no rule — the dispatch path is then
-- exactly what it was before this column existed. The writer stores an empty
-- list as NULL, so "no rule" has one stored form.
--
-- Merchant-level, never per template: a customer limit that changes with
-- which template is calling is not a customer limit.
--
-- The CHECK is on FORMAT only (it must be an array). What an entry may hold
-- (max_calls >= 1, window_hours 1-168, one entry) is validated on write by
-- CallLimit / CallLimitsUpdate (app/schemas/breeze_buddy/merchants.py) and
-- re-validated on every read by the dispatch worker, which fails closed on a
-- row it cannot understand.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS call_limits jsonb;

ALTER TABLE merchants DROP CONSTRAINT IF EXISTS merchants_call_limits_is_array;
ALTER TABLE merchants ADD CONSTRAINT merchants_call_limits_is_array
    CHECK (call_limits IS NULL OR jsonb_typeof(call_limits) = 'array');
