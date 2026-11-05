-- 084: merchants.analytics_field_config — which key in a merchant's JSON fills
-- which ClickHouse analytics column (phase 1 of the analytics move).
--
--   {"calls":  {"<template id> | *": {"<slot>": {"path": "payload.x", "label": "…"}}},
--    "events": {"<topic> | *":       {"<slot>": {"path": "learned.y", "label": "…"}}}}
--
-- Slots: the 7 shared keys (amount, category, reason, product_name, language,
-- region, order_id) and custom_text_1..10 / custom_num_1..5; the slot fixes
-- the type. NULL = no mapping (an empty config is stored as NULL). PeerDB
-- mirrors the column to ClickHouse, where a dictionary reloads it every
-- minute, so keep it small.
--
-- The future page config (charts, queries, tabs) is a separate column,
-- analytics_page_config: read by the console, excluded from the mirror.
--
-- CHECK on format only; content is validated on write by AnalyticsFieldConfig
-- (app/schemas/breeze_buddy/merchants.py). No DROP CONSTRAINT IF EXISTS: a
-- name clash must fail, not replace unseen.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS analytics_field_config jsonb;

ALTER TABLE merchants ADD CONSTRAINT merchants_analytics_field_config_is_object
    CHECK (analytics_field_config IS NULL OR jsonb_typeof(analytics_field_config) = 'object');
