-- Migration 032: Per-turn UI/latency metrics for Breeze Buddy chat mode.
--
-- Additive side-channel for the conversational-log analytics surface
-- (docs/analytics/CHAT_ANALYTICS_PLAN.md, Phase 1B). The chat agent already
-- emits a structured ``[CHAT_METRICS]`` log line per turn (chat/metrics.py);
-- that line is right for ops/aggregate dashboards but can't be joined to an
-- individual message in a product view, and logs age out. This table mirrors
-- the SAME structural metrics into a row keyed by the assistant message's
-- ``idx`` so the log-detail UI can show latency next to each assistant turn
-- via a 1:1 LEFT JOIN against chat_message.
--
-- Pure addition: the hot chat_message insert path and the agent's turn loop
-- are unchanged. The row is written best-effort from the router's existing
-- TurnMetrics object after the turn completes; a write failure never affects
-- the turn (the [CHAT_METRICS] log still fires).
--
-- Structural only — every column is a timing, a count, or a status. No user
-- or tool payload content is stored here (privacy-by-design logging contract).
--
-- ``idx`` is a soft reference to the assistant chat_message(session_id, idx)
-- the turn produced. It is intentionally NOT a composite FK to chat_message:
-- the message and the metrics row are written at different points and the
-- LEFT JOIN tolerates a missing match (e.g. a failed turn with no assistant
-- row). The FK to chat_session keeps the row scoped + cascade-deleted.

CREATE TABLE IF NOT EXISTS chat_turn_metrics (
    session_id      uuid NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    idx             integer NOT NULL,
    ttft_ms         real,
    ttfui_ms        real,
    ttlui_ms        real,
    total_ms        real,
    ui_ops          integer NOT NULL DEFAULT 0,
    ui_dropped      integer NOT NULL DEFAULT 0,
    healer_applied  integer NOT NULL DEFAULT 0,
    tool_calls      integer NOT NULL DEFAULT 0,
    prose_chars     integer NOT NULL DEFAULT 0,
    ui_chars        integer NOT NULL DEFAULT 0,
    status          varchar(20),
    phase           varchar(40) NOT NULL DEFAULT 'baseline',
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, idx)
);

-- Fetch all turn metrics for one session (log-detail join), ordered by idx.
CREATE INDEX IF NOT EXISTS idx_chat_turn_metrics_session
    ON chat_turn_metrics (session_id);
