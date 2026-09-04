-- ===========================================================================
-- 057: ui_component — merchant-scoped custom UI component registry
-- ---------------------------------------------------------------------------
-- CHAMELEON: custom components as DATA. A row defines one merchant-specific
-- component (e.g. Chennai One's JourneyOptions): a JSON-Schema contract for
-- its hydrated props, behavioral flags the engine reads (selection field,
-- list caps), and an OPTIONAL declarative render_def our widget interprets.
-- Backend-only merchants (own frontend) leave render_def NULL.
--
-- Visibility is per-session data: a template opts in via
-- configurations.ui_catalog.custom_components = ["JourneyOptions", ...];
-- the chat agent overlays the named rows onto its session-scoped catalog.
-- Nothing process-global changes — two merchants on the same worker never
-- see each other's definitions.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ui_component (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id   varchar(255) NOT NULL,
    -- NULL = reseller-wide component (usable by any of the reseller's
    -- templates that opt in by name).
    merchant_id   varchar(255),
    -- PascalCase component type, unique per scope (see index below). The
    -- write path additionally rejects collisions with built-in catalog and
    -- flavor component names.
    name          varchar(128) NOT NULL,
    -- Bumped on every update; hydrated ops and def caches key on it.
    version       integer NOT NULL DEFAULT 1,
    -- JSON Schema (draft 2020-12) for the component's hydrated props.
    props_schema  jsonb NOT NULL,
    -- Engine-read behavior: {"data_bound": true, "selection_field": "...",
    -- "list_props": [...], "max_items_default": n, "max_items_limit": n}.
    flags         jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Declarative render tree for OUR widget/skins. NULL for merchants
    -- rendering with their own frontend.
    render_def    jsonb,
    -- One or two sentences spliced into render_ui's bind coaching when the
    -- component is offered ("Use JourneyOptions after search_journeys ...").
    prompt_hint   text,
    is_active     boolean NOT NULL DEFAULT TRUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ui_component_version_positive CHECK (version > 0)
);

-- Uniqueness per (reseller, merchant, name) with NULL merchant folded to ''
-- so a reseller-wide name can't be re-registered as a merchant row twice.
CREATE UNIQUE INDEX IF NOT EXISTS ui_component_scope_name_unique
    ON ui_component (reseller_id, COALESCE(merchant_id, ''), name);
