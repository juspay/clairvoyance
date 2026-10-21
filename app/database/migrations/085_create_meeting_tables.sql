-- Migration: Meeting recording — one row per recording, one row per transcript line
-- Description: Buddy on the desk can be told "start recording the meeting"; from
-- then until the stop phrase every finished sentence is a transcript line and
-- nothing reaches the LLM. These two tables hold that transcript.
--
-- meeting is the unit: who recorded (merchant/reseller), on which connection
-- (lead_id → lead_call_tracker; a reconnect is a new lead but the same
-- meeting), its lifecycle (state, started/ended, duration, why it ended), a
-- heartbeat the bot refreshes every ~30s so a sweeper can tell a live meeting
-- from one whose bot died, and whether the transcript reached its destination
-- (delivery_state). Vocabulary (RECORDING/FINISHED/INTERRUPTED,
-- PENDING/SENT/FAILED, voice/max_duration/stale) lives in code; the CHECKs
-- here pin FORMAT only.
--
-- meeting_segment is append-only: the bot batches lines in memory and inserts
-- them every ~15s with ON CONFLICT DO NOTHING, so a retried batch is a no-op
-- and (meeting_id, seq) is the only index it needs. Timestamps are offsets
-- from meeting.started_at. No transcript text ever goes in logs — it lives
-- only here.
--
-- The partial index is the sweeper's work queue: RECORDING rows ordered by
-- heartbeat, never touching finished meetings.

CREATE TABLE meeting (
    id                 varchar(64)  PRIMARY KEY,
    reseller_id        varchar(255) NOT NULL,
    merchant_id        varchar(100) NOT NULL,
    lead_id            varchar(255) NOT NULL,
    state              varchar(32)  NOT NULL,
    started_at         timestamptz  NOT NULL DEFAULT now(),
    ended_at           timestamptz,
    duration_s         integer,
    end_reason         varchar(32),
    last_heartbeat_at  timestamptz  NOT NULL DEFAULT now(),
    stt_provider       varchar(32)  NOT NULL,
    delivery_state     varchar(32)  NOT NULL DEFAULT 'PENDING',
    delivery_attempts  integer      NOT NULL DEFAULT 0,
    delivery_error     text,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT meeting_state_format_check
        CHECK (state ~ '^[A-Z][A-Z_]{1,31}$'),
    CONSTRAINT meeting_delivery_state_format_check
        CHECK (delivery_state ~ '^[A-Z][A-Z_]{1,31}$'),
    CONSTRAINT meeting_end_reason_format_check
        CHECK (end_reason IS NULL OR end_reason ~ '^[a-z][a-z_]{1,31}$'),
    CONSTRAINT meeting_ended_after_started_check
        CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT meeting_duration_check
        CHECK (duration_s IS NULL OR duration_s >= 0),
    CONSTRAINT meeting_delivery_attempts_check
        CHECK (delivery_attempts >= 0)
);

CREATE INDEX meeting_recording_heartbeat_idx
    ON meeting (last_heartbeat_at)
    WHERE state = 'RECORDING';

CREATE INDEX meeting_lead_id_idx
    ON meeting (lead_id);

CREATE INDEX meeting_merchant_started_idx
    ON meeting (merchant_id, started_at DESC);

CREATE TABLE meeting_segment (
    meeting_id  varchar(64)  NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    seq         integer      NOT NULL,
    speaker     varchar(32),
    text        text         NOT NULL,
    start_ms    integer      NOT NULL,
    end_ms      integer      NOT NULL,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, seq),
    CONSTRAINT meeting_segment_seq_check
        CHECK (seq >= 0),
    CONSTRAINT meeting_segment_text_check
        CHECK (btrim(text) <> ''),
    CONSTRAINT meeting_segment_time_check
        CHECK (start_ms >= 0 AND end_ms >= start_ms)
);
