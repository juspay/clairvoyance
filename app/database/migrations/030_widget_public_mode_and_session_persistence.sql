-- Migration 030: widget public mode + chat-session persistence (consolidated).
--
-- Two feature areas that ship together — splitting them was an accident of
-- branch ordering, not a logical division. Both depend only on tables that
-- existed in 027 (chat_session, chat_message) and add net-new
-- tables/columns; they don't touch each other's surfaces.
--
-- A. Widget public mode (CHAT_MODE.md §14)
-- ----------------------------------------
--   A1. widget_config — per-merchant config used by embedded browser
--       widgets to drive Breeze Buddy chat + voice without an RBAC s2s
--       JWT. The widget snippet carries an opaque public_widget_key
--       (generated via secrets.token_urlsafe(32)); the widget routes
--       resolve it together with the Origin header. Empty allowed_origins
--       means "deny all" so a freshly created row is unusable until an
--       admin populates it.
--   A2. chat_session.current_channel + voice_lead_id — promotes
--       chat_session from "chat-only" to the canonical widget conversation
--       backbone. The conversation reuses ONE lead_call_tracker row
--       across every voice attachment (bumped attempt_count) instead of
--       creating + tearing down a lead per handoff.
--       State machine on current_channel:
--         CHAT  → VOICE  (POST /widget/session/{id}/voice/connect)
--         VOICE → CHAT   (POST /widget/session/{id}/voice/end | end_conversation drain)
--         CHAT  → ENDED  (POST /widget/session/{id}/end)
--         VOICE → ENDED  (POST /widget/session/{id}/end — also forces drain)
--       Backwards compatible: existing rows default to CHAT; legacy chat
--       routes never read current_channel; only the new /widget/session
--       router mutates it.
--
-- B. Chat-session persistence (history + state + UI replay)
-- ---------------------------------------------------------
--   B1. chat_message.content_blocks (JSONB) — canonical Anthropic content
--       array for both roles. Replaces prior prose-only persistence which
--       dropped tool_use / tool_result blocks and caused the LLM to
--       hallucinate identifiers (cart_id, checkout_id) on subsequent
--       turns. The existing `content TEXT` column is retained for
--       transcript export / analytics / non-JSONB callers.
--   B2. agent_session_state — generic per-session state row. Deliberately
--       domain-blind: no cart_id / checkout_id columns. Identifiers live
--       inside the `data` JSONB, populated by template-declared reducers
--       (template/session_state.py). A future vertical (travel, ticketing,
--       …) ships different reducers with zero migration cost. Outgoing
--       MCP tool calls read the same JSONB to inject identifiers.
--   B3. chat_message.ui_blocks (JSONB) — SpecStream `ui_op` events emitted
--       during an assistant turn. Consumed only by the widget resume path
--       (GET /widget/session/{id}): on refresh the widget replays each
--       turn's ui_blocks through the same applyOp pipeline the live SSE
--       stream uses, repainting Tiles / Carousels / cart views without
--       rerunning the LLM. Kept separate from content_blocks so the LLM
--       never sees prior ui_ops on replay (by design).
--
-- All statements use IF NOT EXISTS / catalog-existence guards so the
-- migration is safe to re-run on a partially-applied environment.

BEGIN;

-- ===========================================================================
-- A. Widget public mode
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- A1. widget_config
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS widget_config (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id                     varchar(255) NOT NULL,
    merchant_id                     varchar(255) NOT NULL,
    public_widget_key               varchar(128) NOT NULL UNIQUE,
    template_id                     uuid NOT NULL REFERENCES template(id)
                                        ON DELETE RESTRICT,
    -- Empty array means "deny all" — explicit by default so a freshly
    -- created widget_config can't be used until the admin adds origins.
    allowed_origins                 text[] NOT NULL DEFAULT ARRAY[]::text[],
    -- Chat caps (per-IP, hourly window for sessions/messages; concurrency
    -- is enforced separately at request time).
    max_sessions_per_ip_hour        integer NOT NULL DEFAULT 60,
    max_messages_per_ip_hour        integer NOT NULL DEFAULT 600,
    max_concurrent_per_ip           integer NOT NULL DEFAULT 4,
    -- Voice caps (per-IP, hourly window for connect attempts).
    max_voice_sessions_per_ip_hour  integer NOT NULL DEFAULT 10,
    active                          boolean NOT NULL DEFAULT TRUE,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT widget_config_reseller_merchant_unique
        UNIQUE (reseller_id, merchant_id),
    -- Caps must be non-negative; 0 is interpreted as "disabled" by the
    -- rate limiter (matches services/redis/rate_limit.py:81-86).
    CONSTRAINT widget_config_caps_nonneg CHECK (
        max_sessions_per_ip_hour       >= 0
        AND max_messages_per_ip_hour       >= 0
        AND max_concurrent_per_ip          >= 0
        AND max_voice_sessions_per_ip_hour >= 0
    )
);

-- NOTE: explicit indexes on (public_widget_key) and (reseller_id, merchant_id)
-- removed — the UNIQUE / CONSTRAINT clauses above already produce btree
-- indexes on those exact columns. Adding named indexes would duplicate
-- the storage + write cost with no read benefit. If you need a named
-- handle for ops queries, query pg_index by the constraint-backed
-- index name (e.g. widget_config_public_widget_key_key).


-- ---------------------------------------------------------------------------
-- A2. chat_session.current_channel + voice_lead_id
-- ---------------------------------------------------------------------------

ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS current_channel varchar(20) NOT NULL DEFAULT 'CHAT';

-- The constraint name uses CHECK; Postgres has no IF NOT EXISTS for
-- ADD CONSTRAINT, so guard with a catalog lookup.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chat_session_current_channel_check'
    ) THEN
        ALTER TABLE chat_session
            ADD CONSTRAINT chat_session_current_channel_check
            CHECK (current_channel IN ('CHAT', 'VOICE', 'ENDED'));
    END IF;
END$$;

-- The widget conversation's voice lead. Set once on the first
-- /voice/connect, reused for every subsequent voice attachment via
-- attempt_count on the lead row. Stays non-NULL after the first
-- attachment so we don't have to recreate the lead on each handoff
-- — keeps the conversation to ONE row in lead_call_tracker.
-- Not a FK to lead_call_tracker(id) because lead rows can be
-- archived/cleaned independently of the chat_session lifetime.
ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS voice_lead_id uuid;

-- Operationally useful: find sessions stuck in VOICE for longer than
-- expected (orphan detection / janitor sweep).
CREATE INDEX IF NOT EXISTS idx_chat_session_voice_lead
    ON chat_session (voice_lead_id)
    WHERE voice_lead_id IS NOT NULL;


-- ===========================================================================
-- B. Chat-session persistence
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- B1. chat_message.content_blocks — canonical Anthropic content array
-- ---------------------------------------------------------------------------

ALTER TABLE chat_message
    ADD COLUMN IF NOT EXISTS content_blocks jsonb;

-- GIN index on the content_blocks JSONB array supports recovery +
-- audit queries that look up a turn by Anthropic tool_use_id
-- (e.g. "find the call that minted cart X"). Earlier draft used
-- `(content_blocks->>'tool_use_id')`, which always returns NULL on
-- a JSONB *array* — `->>` only extracts from objects. GIN on the
-- whole column with the default jsonb_ops handles `@> '[{"tool_use_id":"..."}]'`
-- containment queries that audit code actually issues. Predicate
-- keeps the index narrow — rows without blocks are skipped.
CREATE INDEX IF NOT EXISTS idx_chat_message_content_blocks_gin
    ON chat_message USING gin (content_blocks)
    WHERE content_blocks IS NOT NULL;

-- Backfill: synthesise a single-element [text] block array for every
-- pre-migration row that has prose. After this, history replay can use
-- content_blocks unconditionally without a NULL-safety fork.
-- WHERE-clause idempotent on re-run (skips rows already backfilled).
UPDATE chat_message
    SET content_blocks = jsonb_build_array(
        jsonb_build_object('type', 'text', 'text', content)
    )
    WHERE content_blocks IS NULL
      AND content IS NOT NULL;


-- ---------------------------------------------------------------------------
-- B2. agent_session_state — generic per-session domain-blind state
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_session_state (
    chat_session_id  uuid PRIMARY KEY REFERENCES chat_session(id) ON DELETE CASCADE,
    data             jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- updated_at index for any future TTL / staleness sweepers.
CREATE INDEX IF NOT EXISTS idx_agent_session_state_updated_at
    ON agent_session_state (updated_at);


-- ---------------------------------------------------------------------------
-- B3. chat_message.ui_blocks — SpecStream ui_op replay
-- ---------------------------------------------------------------------------

ALTER TABLE chat_message
    ADD COLUMN IF NOT EXISTS ui_blocks JSONB DEFAULT NULL;

COMMENT ON COLUMN chat_message.ui_blocks IS
    'SpecStream ui_op list emitted during this assistant turn. NULL on user '
    'turns and on assistant turns that emitted no UI. Used by the widget '
    'resume path to repaint Tiles/Carousels after a page reload without '
    'rerunning the LLM. Independent of content_blocks (which is the canonical '
    'Anthropic-shape history the LLM sees on subsequent turns).';

COMMIT;
