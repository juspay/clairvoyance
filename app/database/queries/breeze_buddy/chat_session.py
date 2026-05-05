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

_SESSION_COLUMNS = """
    id, template_id, reseller_id, merchant_id,
    status, outcome, current_node, metadata,
    created_at, last_activity_at, ended_at, ended_reason
"""

_MESSAGE_COLUMNS = """
    session_id, idx, role, content, created_at
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


# -- chat_message -------------------------------------------------------------


def insert_chat_message_query(
    session_id: str,
    role: str,
    content: Optional[str],
) -> Tuple[str, List[Any]]:
    """Insert one message with auto-allocated idx (per-session monotonic).

    Idx allocation is a subquery on chat_message itself, so the INSERT
    is atomic. Combined with the per-session Redis lock and the
    (session_id, idx) PK, duplicates are impossible.
    """
    query = f"""
        INSERT INTO {CHAT_MESSAGE_TABLE} (
            session_id, idx, role, content
        )
        VALUES (
            $1,
            COALESCE(
                (SELECT MAX(idx) + 1
                 FROM {CHAT_MESSAGE_TABLE}
                 WHERE session_id = $1),
                0
            ),
            $2, $3
        )
        RETURNING {_MESSAGE_COLUMNS}
    """
    return query, [session_id, role, content]


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
