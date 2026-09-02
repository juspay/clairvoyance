-- Buddy Human Assist persistence and platform orchestration.
--
-- A Human Assist ticket is the chat_session that owns its transcript. Repeat
-- handoffs roll into a new session, so there is no separate ticket table or
-- second identifier. Lifecycle and platform state live under
-- chat_session.metadata.human_assist.
-- The platform key is not constrained to an enum list so future adapters do
-- not require a schema migration.

BEGIN;

ALTER TABLE chat_message
    ADD COLUMN IF NOT EXISTS sender_type varchar(20);

COMMENT ON COLUMN chat_message.sender_type IS
    'Human Assist message attribution: customer, buddy, human, system, or '
    'internal. NULL for ordinary (non-Live-Assist) chat messages; human rows '
    'are excluded from AI evaluation.';

ALTER TABLE chat_message
    ADD CONSTRAINT chat_message_sender_type_check
    CHECK (
        sender_type IS NULL
        OR sender_type IN ('customer', 'buddy', 'human', 'system', 'internal')
    );

ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS handoff_happened boolean NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN chat_session.handoff_happened IS
    'Durable fact that at least one Human Assist handoff was requested in this session.';

ALTER TABLE widget_config
    ADD COLUMN IF NOT EXISTS human_assist_enabled boolean NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN widget_config.human_assist_enabled IS
    'Per-merchant switch controlling whether Buddy may create native Human Assist tickets.';

ALTER TABLE widget_config
    ADD COLUMN IF NOT EXISTS human_assist_platform varchar(64)
    NOT NULL DEFAULT 'native';

COMMENT ON COLUMN widget_config.human_assist_platform IS
    'Registered Human Assist adapter used for new handoffs. Existing active '
    'conversations retain the platform snapshot in their metadata JSONB.';

-- Replace the migration-030 channel constraint so HUMAN becomes a first-class
-- routing state alongside CHAT and VOICE.
ALTER TABLE chat_session
    DROP CONSTRAINT IF EXISTS chat_session_current_channel_check;

ALTER TABLE chat_session
    ADD CONSTRAINT chat_session_current_channel_check
    CHECK (current_channel IN ('CHAT', 'VOICE', 'HUMAN', 'ENDED'));

ALTER TABLE chat_session
    DROP CONSTRAINT IF EXISTS chat_session_ended_reason_check;

ALTER TABLE chat_session
    ADD CONSTRAINT chat_session_ended_reason_check CHECK (
        ended_reason IS NULL
        OR ended_reason IN (
            'user_ended',
            'idle_timeout',
            'human_assist_rollover'
        )
    );

ALTER TABLE chat_session
    ADD CONSTRAINT chat_session_human_assist_record_check CHECK (
        metadata->'human_assist' IS NULL
        OR (
            jsonb_typeof(metadata->'human_assist') = 'object'
            AND metadata #>> '{human_assist,status}' IN (
                'PENDING', 'OPEN', 'CLOSED', 'TIMED_OUT'
            )
            AND NULLIF(
                metadata #>> '{human_assist,widget_config_id}',
                ''
            ) IS NOT NULL
            AND NULLIF(
                metadata #>> '{human_assist,requested_at}',
                ''
            ) IS NOT NULL
            AND NULLIF(
                metadata #>> '{human_assist,claim_deadline_at}',
                ''
            ) IS NOT NULL
            AND NULLIF(
                metadata #>> '{human_assist,customer_last_seen_at}',
                ''
            ) IS NOT NULL
            AND (
                metadata #>> '{human_assist,close_reason}' IS NULL
                OR metadata #>> '{human_assist,close_reason}' IN (
                    'merchant_closed',
                    'claim_timeout',
                    'customer_disconnected',
                    'session_ended',
                    'platform_closed',
                    'platform_error'
                )
            )
            AND (
                (
                    metadata #>> '{human_assist,status}' IN ('PENDING', 'OPEN')
                    AND metadata #>> '{human_assist,closed_at}' IS NULL
                )
                OR (
                    metadata #>> '{human_assist,status}' IN (
                        'CLOSED', 'TIMED_OUT'
                    )
                    AND metadata #>> '{human_assist,closed_at}' IS NOT NULL
                )
            )
        ) IS TRUE
    );

ALTER TABLE chat_session
    ADD CONSTRAINT chat_session_handoff_record_check CHECK (
        handoff_happened = FALSE
        OR metadata->'human_assist' IS NOT NULL
    );

COMMENT ON COLUMN chat_session.metadata IS
    'Session metadata. Human Assist sessions store their ticket lifecycle under '
    'the human_assist key; the chat_session UUID is also the Human Assist ID.';

CREATE INDEX idx_chat_session_human_assist_inbox
    ON chat_session (
        reseller_id,
        merchant_id,
        ((metadata #>> '{human_assist,status}')),
        last_activity_at DESC
    )
    WHERE metadata ? 'human_assist';

CREATE INDEX idx_chat_session_human_assist_claim_deadline
    ON chat_session ((metadata #>> '{human_assist,claim_deadline_at}'))
    WHERE metadata #>> '{human_assist,status}' = 'PENDING';

CREATE INDEX idx_chat_session_human_assist_customer_seen
    ON chat_session ((metadata #>> '{human_assist,customer_last_seen_at}'))
    WHERE metadata #>> '{human_assist,status}' IN ('PENDING', 'OPEN');

COMMENT ON INDEX idx_chat_session_human_assist_claim_deadline IS
    'Supports ordered pending-ticket claim-timeout sweeps.';

COMMENT ON INDEX idx_chat_session_human_assist_customer_seen IS
    'Supports ordered disconnected-customer sweeps.';

COMMIT;
