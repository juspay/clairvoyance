"""SQL builders for chat_session and chat_message tables.

This module contains ONLY query generation functions. Business logic
lives in accessor/breeze_buddy/chat_session.py. JSON serialization is
the caller's responsibility — query builders treat JSONB values as
opaque strings (matches the existing call_execution_config pattern).
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

CHAT_SESSION_TABLE = "chat_session"
CHAT_MESSAGE_TABLE = "chat_message"
AGENT_SESSION_STATE_TABLE = "agent_session_state"

_SESSION_COLUMNS = """
    id, template_id, reseller_id, merchant_id,
    status, outcome, current_node, metadata,
    current_channel, voice_lead_id,
    created_at, last_activity_at, ended_at, ended_reason
"""

_MESSAGE_COLUMNS = """
    session_id, idx, role, content, content_blocks, ui_blocks, created_at
"""

_AGENT_STATE_COLUMNS = """
    chat_session_id, data, updated_at
"""


# -- chat_session -------------------------------------------------------------


def create_chat_session_query(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    metadata_json: str,
) -> Tuple[str, List[Any]]:
    """Insert a new ACTIVE session, returning the full row."""
    query = f"""
        INSERT INTO {CHAT_SESSION_TABLE} (
            template_id, reseller_id, merchant_id, metadata
        )
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [template_id, reseller_id, merchant_id, metadata_json]


def get_chat_session_by_id_query(session_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_SESSION_COLUMNS}
        FROM {CHAT_SESSION_TABLE}
        WHERE id = $1
    """
    return query, [session_id]


def update_chat_session_after_turn_query(
    session_id: str,
    current_node: Optional[str],
) -> Tuple[str, List[Any]]:
    """Single post-turn UPDATE: bump activity + (optionally) set current_node.

    Used at the tail of every successful chat turn. ``current_node``
    may be ``None`` for turns that didn't transition (or for direct-mode
    templates) — in that case the column is left as-is via COALESCE.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET current_node = COALESCE($1, current_node),
            last_activity_at = now()
        WHERE id = $2
    """
    return query, [current_node, session_id]


def end_chat_session_query(
    session_id: str,
    outcome: Optional[str],
    ended_reason: str,
) -> Tuple[str, List[Any]]:
    """Idempotent end: only flips status if not already ENDED.

    `ended_at` and `ended_reason` are preserved if already set, so a
    retry doesn't overwrite the original ending event.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET status = 'ENDED',
            outcome = COALESCE($1, outcome),
            ended_at = COALESCE(ended_at, now()),
            ended_reason = COALESCE(ended_reason, $2),
            last_activity_at = now()
        WHERE id = $3 AND status <> 'ENDED'
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [outcome, ended_reason, session_id]


def list_idle_chat_sessions_query(
    cutoff: datetime,
    statuses: List[str],
    limit: int = 100,
) -> Tuple[str, List[Any]]:
    """Sessions whose last_activity_at < cutoff and status ∈ statuses.

    Ordered ascending so the oldest are processed first by the sweeper.
    """
    query = f"""
        SELECT {_SESSION_COLUMNS}
        FROM {CHAT_SESSION_TABLE}
        WHERE status = ANY($1)
          AND last_activity_at < $2
        ORDER BY last_activity_at ASC
        LIMIT $3
    """
    return query, [statuses, cutoff, limit]


# -- widget-mode channel state mutations (migration 030) --------------------


def set_chat_session_voice_lead_query(
    session_id: str, voice_lead_id: str
) -> Tuple[str, List[Any]]:
    """First-time bind of a voice lead to a chat_session.

    Conditioned on ``voice_lead_id IS NULL`` so a concurrent
    /voice/connect can't overwrite an already-bound lead. Returns the
    row when the bind succeeded, empty if the row already had a
    voice_lead_id (caller should reuse the existing one).
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET voice_lead_id = $2::uuid,
            last_activity_at = now()
        WHERE id = $1 AND voice_lead_id IS NULL
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [session_id, voice_lead_id]


def flip_chat_session_channel_query(
    session_id: str,
    *,
    new_channel: str,
    expected_channel: str,
    expected_voice_lead_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Conditionally flip current_channel.

    The conditional WHERE gives the unified widget router a
    single-statement state machine — the row only updates when its
    current state matches the caller's expectation. This makes
    /voice/connect, /voice/end, and the drain race-free even if
    multiple workers attempt the same transition concurrently.

    ``expected_channel`` MUST match. Pass the channel the caller
    believes it's leaving ('CHAT' for /voice/connect; 'VOICE' for
    /voice/end and drain).

    ``expected_voice_lead_id`` (optional) — when set, also requires
    the row's voice_lead_id to match. Used by drain so a stale
    end_conversation callback for an old lead can't flip a
    freshly-reused session back to CHAT.

    Returns the row when the UPDATE succeeded, empty when no row
    matched (callers should treat empty as a 409 conflict).
    """
    where_clauses: List[str] = [
        "id = $1",
        "current_channel = $3",
    ]
    params: List[Any] = [session_id, new_channel, expected_channel]
    next_idx = 4

    if expected_voice_lead_id is not None:
        where_clauses.append(f"voice_lead_id = ${next_idx}::uuid")
        params.append(expected_voice_lead_id)
        next_idx += 1

    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET current_channel = $2,
            last_activity_at = now()
        WHERE {' AND '.join(where_clauses)}
        RETURNING {_SESSION_COLUMNS}
    """
    return query, params


def drain_voice_into_chat_session_query(
    session_id: str,
    *,
    final_node: Optional[str],
    expected_voice_lead_id: str,
) -> Tuple[str, List[Any]]:
    """Final UPDATE at the end of a voice attachment.

    Flips channel back to CHAT, optionally advances current_node to
    whatever node the voice flow ended on. Does NOT clear
    voice_lead_id — the lead stays bound so the next
    /voice/connect on this session can reuse it.

    Conditioned on (current_channel='VOICE' AND voice_lead_id matches)
    so a stale callback for an old lead can't flip a freshly-reused
    session back to CHAT.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET current_channel = 'CHAT',
            current_node    = COALESCE($1, current_node),
            last_activity_at = now()
        WHERE id = $2
          AND current_channel = 'VOICE'
          AND voice_lead_id = $3::uuid
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [final_node, session_id, expected_voice_lead_id]


# -- chat_message -------------------------------------------------------------


def insert_chat_message_query(
    session_id: str,
    role: str,
    content: Optional[str],
    content_blocks_json: Optional[str] = None,
    ui_blocks_json: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Insert one message with auto-allocated idx (per-session monotonic).

    Idx allocation is a subquery on chat_message itself, so the INSERT
    is atomic. Combined with the per-session Redis lock and the
    (session_id, idx) PK, duplicates are impossible.

    ``content_blocks_json`` is the canonical Anthropic content array
    serialised to a JSON string. When supplied, the loader's history
    replay path uses it verbatim; ``content`` is kept as a denormalised
    prose-only view for transcripts/analytics.

    ``ui_blocks_json`` (migration 030) is the SpecStream ui_op list
    serialised to a JSON string. Consumed only by the widget resume
    path to repaint Tiles/Carousels; the LLM never sees this column.
    """
    query = f"""
        INSERT INTO {CHAT_MESSAGE_TABLE} (
            session_id, idx, role, content, content_blocks, ui_blocks
        )
        VALUES (
            $1,
            COALESCE(
                (SELECT MAX(idx) + 1
                 FROM {CHAT_MESSAGE_TABLE}
                 WHERE session_id = $1),
                0
            ),
            $2, $3, $4::jsonb, $5::jsonb
        )
        RETURNING {_MESSAGE_COLUMNS}
    """
    return query, [session_id, role, content, content_blocks_json, ui_blocks_json]


def list_chat_messages_for_session_query(
    session_id: str,
    limit: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Message log for a session, ordered by idx ASC.

    When ``limit`` is set, returns only the most recent ``limit`` rows
    (still ordered ascending) — used by the per-turn LLM-context replay
    where unbounded reads would scale with conversation length. ``None``
    returns the full log (used by transcript export and resume API).
    """
    if limit is None:
        query = f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM {CHAT_MESSAGE_TABLE}
            WHERE session_id = $1
            ORDER BY idx ASC
        """
        return query, [session_id]

    # Take the last ``limit`` rows (DESC + LIMIT) then re-sort ASC so
    # the caller gets chronological order.
    query = f"""
        SELECT {_MESSAGE_COLUMNS} FROM (
            SELECT {_MESSAGE_COLUMNS}
            FROM {CHAT_MESSAGE_TABLE}
            WHERE session_id = $1
            ORDER BY idx DESC
            LIMIT $2
        ) recent
        ORDER BY idx ASC
    """
    return query, [session_id, limit]


# -- agent_session_state ------------------------------------------------------


def get_agent_session_state_query(chat_session_id: str) -> Tuple[str, List[Any]]:
    """Read the agent state row for a session. Returns no rows if absent.

    Generic table — `data` JSONB holds whatever keys the template's
    reducers set. Runtime knows nothing about the keys.
    """
    query = f"""
        SELECT {_AGENT_STATE_COLUMNS}
        FROM {AGENT_SESSION_STATE_TABLE}
        WHERE chat_session_id = $1
    """
    return query, [chat_session_id]


def upsert_agent_session_state_query(
    chat_session_id: str,
    data_json: str,
) -> Tuple[str, List[Any]]:
    """INSERT-or-REPLACE the entire `data` JSONB for a session.

    The reducer engine merges + computes the next state from the prior
    state + tool result in Python, then this query persists the result
    atomically. No SQL-level merge — keeps the engine pure.
    """
    query = f"""
        INSERT INTO {AGENT_SESSION_STATE_TABLE} (chat_session_id, data, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (chat_session_id) DO UPDATE
            SET data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at
        RETURNING {_AGENT_STATE_COLUMNS}
    """
    return query, [chat_session_id, data_json]
