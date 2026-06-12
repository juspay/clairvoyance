-- Migration 033: HITL tool-approval state for approval-gated global functions.
--
-- A template author can mark a global function (HTTP/builtin/custom) with an
-- ``approval`` config; when the LLM calls it in CHAT mode the turn ends at the
-- gate and a PENDING row is written here. The decision arrives later on the
-- dedicated ``POST .../session/{id}/approval`` endpoint, which atomically
-- claims the row (UPDATE ... WHERE status='PENDING') and resumes the turn.
--
-- Why a table (not Redis/memory): the paired tool_use block is already
-- durably persisted in chat_message at gate time — until a tool_result row
-- answers it, that history is invalid to replay. This row is the durable
-- record that lets the dangling history be repaired (it holds tool_call_id /
-- function_name / arguments), keeps expiry as a READABLE STATE (a late
-- click gets "already timed out", not a 404), and is the audit trail for
-- functions the author explicitly marked dangerous. Claim and tool_result
-- insert are two statements under the per-session Redis lock; the
-- claim-FIRST ordering keeps retries safe (a crash between them is healed
-- in-memory by block_codec.repair_dangling_tool_uses on the next replay).
--
-- The table is channel-generic (``channel`` column) so a future voice audit
-- trail can reuse it; in v1 only the chat path writes rows (voice approvals
-- are in-process futures with no DB state). ``session_id`` therefore stays a
-- NOT NULL FK to chat_session for now.
--
-- Expiry is lazy — no sweeper. Nothing is blocked server-side while a row is
-- PENDING; stale rows are claimed (EXPIRED/SUPERSEDED) the next time the
-- session is touched, and die with the session via CASCADE.

CREATE TABLE IF NOT EXISTS tool_approvals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      uuid NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    channel         varchar(10) NOT NULL DEFAULT 'CHAT'
        CHECK (channel IN ('CHAT', 'VOICE')),
    tool_call_id    varchar(128) NOT NULL,
    function_name   varchar(255) NOT NULL,
    arguments       jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt          text,
    status          varchar(20) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'SUPERSEDED')),
    reason          text,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz,
    expires_at      timestamptz NOT NULL,

    UNIQUE (session_id, tool_call_id)
);

-- Pending lookups are the hot path (per-turn dangling resolution + widget
-- resume state); the partial index keeps them O(pending), not O(history).
CREATE INDEX IF NOT EXISTS idx_tool_approvals_session_pending
    ON tool_approvals (session_id)
    WHERE status = 'PENDING';
