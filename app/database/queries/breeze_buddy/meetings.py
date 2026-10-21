"""Query builders for meeting recording (meeting + meeting_segment).

All functions are pure: they return ``(sql, values)`` with ``$n`` placeholders
and perform no I/O. Execution lives in the accessor.

State transitions are single UPDATEs guarded by ``WHERE state = <expected>``
so a repeated command (double stop, double start) matches nothing instead of
corrupting the row. Segment appends are multi-row inserts with
``ON CONFLICT DO NOTHING``: a retried batch is a no-op.
"""

from typing import Any, List, Sequence, Tuple

from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    MeetingEndReason,
    MeetingState,
    SegmentLine,
)

MEETING_TABLE = "meeting"
MEETING_SEGMENT_TABLE = "meeting_segment"

_MEETING_COLUMNS = """
    id, reseller_id, merchant_id, lead_id, state, started_at, ended_at,
    duration_s, end_reason, last_heartbeat_at, stt_provider, delivery_state,
    delivery_attempts, delivery_error, created_at, updated_at
"""

_SEGMENT_COLUMNS = "meeting_id, seq, speaker, text, start_ms, end_ms"


# ─────────────────────────────────────────────────────────────────────────────
# meeting
# ─────────────────────────────────────────────────────────────────────────────


def create_meeting_query(
    meeting_id: str,
    reseller_id: str,
    merchant_id: str,
    lead_id: str,
    stt_provider: str,
) -> Tuple[str, List[Any]]:
    """Insert a new RECORDING meeting; started_at and heartbeat default to now()."""
    query = f"""
        INSERT INTO {MEETING_TABLE} (
            id, reseller_id, merchant_id, lead_id, state, stt_provider
        ) VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_MEETING_COLUMNS}
    """
    return query, [
        meeting_id,
        reseller_id,
        merchant_id,
        lead_id,
        MeetingState.RECORDING.value,
        stt_provider,
    ]


def get_meeting_by_id_query(meeting_id: str, merchant_id: str) -> Tuple[str, List[Any]]:
    """One meeting, merchant-scoped: a token for merchant A never reads B's row."""
    query = f"""
        SELECT {_MEETING_COLUMNS}
        FROM {MEETING_TABLE}
        WHERE id = $1 AND merchant_id = $2
    """
    return query, [meeting_id, merchant_id]


def get_meeting_query(meeting_id: str) -> Tuple[str, List[Any]]:
    """One meeting by id, unscoped: the API checks tenant access on the row."""
    query = f"""
        SELECT {_MEETING_COLUMNS}
        FROM {MEETING_TABLE}
        WHERE id = $1
    """
    return query, [meeting_id]


def get_meetings_by_lead_query(lead_id: str) -> Tuple[str, List[Any]]:
    """Every meeting recorded on one connection, newest first."""
    query = f"""
        SELECT {_MEETING_COLUMNS}
        FROM {MEETING_TABLE}
        WHERE lead_id = $1
        ORDER BY started_at DESC
    """
    return query, [lead_id]


def get_recording_meeting_by_lead_query(lead_id: str) -> Tuple[str, List[Any]]:
    """The meeting still RECORDING on this connection, if any.

    ``start_meeting_recording`` asks this first so a repeated "start" returns
    the running meeting instead of opening a second one.
    """
    query = f"""
        SELECT {_MEETING_COLUMNS}
        FROM {MEETING_TABLE}
        WHERE lead_id = $1 AND state = $2
        ORDER BY started_at DESC
        LIMIT 1
    """
    return query, [lead_id, MeetingState.RECORDING.value]


def heartbeat_meeting_query(meeting_id: str) -> Tuple[str, List[Any]]:
    """Refresh the liveness stamp; only a RECORDING meeting has a pulse."""
    query = f"""
        UPDATE {MEETING_TABLE}
        SET last_heartbeat_at = now(), updated_at = now()
        WHERE id = $1 AND state = $2
        RETURNING id
    """
    return query, [meeting_id, MeetingState.RECORDING.value]


def finish_meeting_query(
    meeting_id: str,
    end_reason: MeetingEndReason,
    final_state: MeetingState = MeetingState.FINISHED,
) -> Tuple[str, List[Any]]:
    """Close a RECORDING meeting exactly once.

    Returns the updated row (all ``_MEETING_COLUMNS``) or no row when the
    meeting was not RECORDING — the caller treats "no row" as "someone
    already stopped it" and says nothing twice.

    ``final_state`` is FINISHED for a voice/max-duration stop and
    INTERRUPTED when the sweeper closes a meeting whose bot died.
    """
    query = f"""
        UPDATE {MEETING_TABLE}
        SET state = $2,
            ended_at = now(),
            duration_s = GREATEST(
                0, EXTRACT(EPOCH FROM (now() - started_at))::integer
            ),
            end_reason = $3,
            updated_at = now()
        WHERE id = $1 AND state = $4
        RETURNING {_MEETING_COLUMNS}
    """
    return query, [
        meeting_id,
        final_state.value,
        end_reason.value,
        MeetingState.RECORDING.value,
    ]


def set_delivery_state_query(
    meeting_id: str,
    delivery_state: DeliveryState,
    error: str | None = None,
) -> Tuple[str, List[Any]]:
    """Record one delivery attempt's outcome; attempts only ever grow."""
    query = f"""
        UPDATE {MEETING_TABLE}
        SET delivery_state = $2,
            delivery_attempts = delivery_attempts + 1,
            delivery_error = $3,
            updated_at = now()
        WHERE id = $1
        RETURNING id
    """
    return query, [meeting_id, delivery_state.value, error]


def get_stale_recording_meetings_query(
    older_than_seconds: int, limit: int = 100
) -> Tuple[str, List[Any]]:
    """RECORDING meetings whose heartbeat is older than the grace window.

    Served by the partial index ``meeting_recording_heartbeat_idx``; finished
    meetings are never scanned.
    """
    query = f"""
        SELECT {_MEETING_COLUMNS}
        FROM {MEETING_TABLE}
        WHERE state = $1
          AND last_heartbeat_at < now() - make_interval(secs => $2)
        ORDER BY last_heartbeat_at
        LIMIT $3
    """
    return query, [MeetingState.RECORDING.value, older_than_seconds, limit]


# ─────────────────────────────────────────────────────────────────────────────
# meeting_segment
# ─────────────────────────────────────────────────────────────────────────────


def append_segments_query(
    meeting_id: str, start_seq: int, lines: Sequence[SegmentLine]
) -> Tuple[str, List[Any]]:
    """One multi-row INSERT for a batch of lines numbered from ``start_seq``.

    Idempotent on retry: ``ON CONFLICT (meeting_id, seq) DO NOTHING``. An
    empty batch raises — the caller must not spend a DB hop on nothing.
    """
    if not lines:
        raise ValueError("append_segments_query: empty batch")

    values: List[Any] = [meeting_id]
    rows: List[str] = []
    for offset, line in enumerate(lines):
        base = len(values)
        rows.append(
            f"($1, ${base + 1}, ${base + 2}, ${base + 3}, ${base + 4}, ${base + 5})"
        )
        values.extend(
            [start_seq + offset, line.speaker, line.text, line.start_ms, line.end_ms]
        )

    query = f"""
        INSERT INTO {MEETING_SEGMENT_TABLE} ({_SEGMENT_COLUMNS})
        VALUES {", ".join(rows)}
        ON CONFLICT (meeting_id, seq) DO NOTHING
    """
    return query, values


def get_segments_query(meeting_id: str) -> Tuple[str, List[Any]]:
    """Every line of one meeting, in order."""
    query = f"""
        SELECT {_SEGMENT_COLUMNS}
        FROM {MEETING_SEGMENT_TABLE}
        WHERE meeting_id = $1
        ORDER BY seq
    """
    return query, [meeting_id]


def get_last_seq_query(meeting_id: str) -> Tuple[str, List[Any]]:
    """Highest seq stored, or -1 for a meeting with no lines yet (resume)."""
    query = f"""
        SELECT COALESCE(MAX(seq), -1) AS last_seq
        FROM {MEETING_SEGMENT_TABLE}
        WHERE meeting_id = $1
    """
    return query, [meeting_id]
