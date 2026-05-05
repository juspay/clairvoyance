-- Migration 026: Chat session and message tables for Breeze Buddy chat (text) mode.
--
-- Two tables:
--   chat_session  — one row per chat session (lifecycle, current flow node, snapshot
--                   of accumulated flow state). Keyed by UUID exposed to clients.
--   chat_message  — append-only message log. Insert-only per turn (no rewrites)
--                   so write cost is O(1) regardless of conversation length.
--
-- Plus an ALTER on `template` to add `supported_channels`, the per-template
-- opt-in that gates which channels (voice, chat) the template can be served on.
-- Defaults to {'voice'} so existing rows keep their current behaviour.
--
-- Design reference: docs/CHAT_MODE.md §5 / §8.
-- Lifecycle is fundamentally different from voice (inbound, on-demand, resumable,
-- no cron, no dial retry), so this is a separate entity from lead_call_tracker.
-- Outcome/transcript shape mirrors voice so webhooks and analytics see uniform
-- "interaction" records across channels.

ALTER TABLE template
    ADD COLUMN supported_channels text[] NOT NULL DEFAULT ARRAY['voice']::text[];

-- ``cardinality()`` returns 0 for empty arrays; ``array_length`` returns
-- NULL, and a NULL CHECK predicate passes — which would silently let an
-- empty ``supported_channels`` slip in. Use ``cardinality`` so the
-- constraint actually rejects ``'{}'``.
ALTER TABLE template
    ADD CONSTRAINT template_supported_channels_check CHECK (
        cardinality(supported_channels) >= 1
        AND supported_channels <@ ARRAY['voice', 'chat']::text[]
    );

CREATE TABLE chat_session (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id       uuid NOT NULL REFERENCES template(id) ON DELETE RESTRICT,
    reseller_id       varchar(255) NOT NULL,
    merchant_id       varchar(255),
    status            varchar(20) NOT NULL DEFAULT 'ACTIVE',
    outcome           varchar(50),
    current_node      varchar(255),
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    ended_at          timestamptz,
    ended_reason      varchar(50),
    CONSTRAINT chat_session_status_check
        CHECK (status IN ('ACTIVE', 'IDLE', 'ENDED')),
    CONSTRAINT chat_session_ended_reason_check
        CHECK (
            ended_reason IS NULL
            OR ended_reason IN ('user_ended', 'idle_timeout')
        )
);

-- Dashboard listing: most recent active sessions by tenant.
CREATE INDEX idx_chat_session_dashboard
    ON chat_session (reseller_id, merchant_id, status, last_activity_at DESC);

-- Idle sweeper: find active/idle sessions whose last_activity_at exceeded TTL.
CREATE INDEX idx_chat_session_idle_sweep
    ON chat_session (status, last_activity_at);

-- Analytics by template.
CREATE INDEX idx_chat_session_template_id
    ON chat_session (template_id);


CREATE TABLE chat_message (
    session_id   uuid NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    idx          integer NOT NULL,
    role         varchar(20) NOT NULL,
    content      text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, idx),
    CONSTRAINT chat_message_role_check
        CHECK (role IN ('user', 'assistant'))
);
