-- 061: widget_config.appearance — the embed's look, next to the embed.
--
-- The storefront widget's appearance (colour, logos, header title, launcher
-- label, offsets) has lived in the Nautilus app database, which makes it a
-- Shopify-specific fact. It isn't: it belongs to the embed, and the embed is
-- this row. widget_config is already the per-merchant unit — UNIQUE
-- (reseller_id, merchant_id), while template_id carries no unique constraint —
-- so branding here keeps one behaviour template shareable across differently
-- branded storefronts.
--
-- Deliberately an OPAQUE jsonb blob. Clairvoyance stores and returns it
-- verbatim and never reads a key out of it; the producer (Nautilus) and the
-- consumer (the widget) are the two ends that agree on names. A tenth
-- appearance field later is then a zero-change migration on this side.
--
-- DEFAULT '{}' is load-bearing, not cosmetic: empty means "no appearance
-- configured here", which is exactly the signal the settings endpoint uses to
-- fall back to the Nautilus row. Existing rows get it for free, so this is an
-- add-column with no backfill and no read-path change for merchants who
-- predate it.

ALTER TABLE widget_config
    ADD COLUMN IF NOT EXISTS appearance jsonb NOT NULL DEFAULT '{}'::jsonb;
