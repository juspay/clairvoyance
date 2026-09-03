-- 066_add_appearance_to_widget_config.sql
--
-- Widget appearance moves onto the widget_config row.
--
-- Until now the storefront look of the Assist widget (colors, header title,
-- launcher label, logos, offsets, modes) lived in the Shopify-app layer
-- (nautilus `buddy_assist_settings`), which made nautilus a mandatory hop in
-- the storefront runtime path just to dress the widget. The widget_config row
-- already owns everything else the loader needs (public key, origins, active),
-- so appearance belongs here too — one row, one owner, and the public
-- storefront-config endpoint can serve the whole mount payload.
--
-- JSONB rather than columns: the appearance vocabulary is display-only,
-- validated at the API boundary (schemas/breeze_buddy/widget_config.py), and
-- expected to grow with the widget SDK; none of it is ever queried by key.

ALTER TABLE widget_config
    ADD COLUMN IF NOT EXISTS appearance jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN widget_config.appearance IS
    'Display-only widget appearance (primary_color, header_title, launcher_label, '
    'header_logo_url, launcher_logo_url, offset_x, offset_y, modes, default_mode). '
    'Validated at the API boundary; served verbatim to the storefront loader.';
