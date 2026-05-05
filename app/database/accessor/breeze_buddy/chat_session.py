"""Async accessors for chat_session and chat_message.

Calls into queries/breeze_buddy/chat_session.py for SQL and
decoder/breeze_buddy/chat_session.py for row → Pydantic.
JSON serialization for JSONB columns happens here.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.chat_session import (
    decode_chat_message,
    decode_chat_session,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.chat_session import (
    create_chat_session_query,
    end_chat_session_query,
    get_chat_session_by_id_query,
    insert_chat_message_query,
    list_chat_messages_for_session_query,
    list_idle_chat_sessions_query,
    update_chat_session_after_turn_query,
)
from app.schemas.breeze_buddy.chat import (
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
)

# -- chat_session -------------------------------------------------------------


async def create_chat_session(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[ChatSession]:
    """Insert a new ACTIVE chat session and return the full row."""
    query, values = create_chat_session_query(
        template_id=template_id,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        metadata_json=json.dumps(metadata or {}),
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            session = decode_chat_session(row)
            if session:
                logger.info(
                    f"Created chat session {session.id} "
                    f"(template={template_id}, reseller={reseller_id})"
                )
            return session
        return None
    except Exception as e:
        logger.error(f"Error creating chat session for template {template_id}: {e}")
        raise


async def get_chat_session_by_id(session_id: str) -> Optional[ChatSession]:
    query, values = get_chat_session_by_id_query(session_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_session(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching chat session {session_id}: {e}")
        raise


async def update_chat_session_after_turn(
    session_id: str,
    current_node: Optional[str] = None,
) -> None:
    """Single post-turn UPDATE: bump activity + (optionally) set current_node.

    Pass ``current_node=None`` for turns that didn't transition or for
    direct-mode templates — the column is preserved via COALESCE.
    """
    query, values = update_chat_session_after_turn_query(session_id, current_node)
    try:
        await run_parameterized_query(query, values)
    except Exception as e:
        logger.error(f"Error updating chat session {session_id} after turn: {e}")
        raise


async def end_chat_session(
    session_id: str,
    ended_reason: str,
    outcome: Optional[str] = None,
) -> Optional[ChatSession]:
    """Idempotent end. Returns the updated row, or None if already ENDED."""
    query, values = end_chat_session_query(
        session_id=session_id,
        outcome=outcome,
        ended_reason=ended_reason,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            session = decode_chat_session(row)
            if session:
                logger.info(f"Ended chat session {session_id} (reason={ended_reason})")
            return session
        return None
    except Exception as e:
        logger.error(f"Error ending chat session {session_id}: {e}")
        raise


async def list_idle_chat_sessions(
    cutoff: datetime,
    statuses: List[ChatSessionStatus],
    limit: int = 100,
) -> List[ChatSession]:
    """Find sessions whose last_activity_at < cutoff and status ∈ statuses."""
    query, values = list_idle_chat_sessions_query(
        cutoff=cutoff,
        statuses=[s.value for s in statuses],
        limit=limit,
    )
    try:
        rows = await run_parameterized_query(query, values)
        sessions: List[ChatSession] = []
        for row in rows or []:
            decoded = decode_chat_session(row)
            if decoded:
                sessions.append(decoded)
        return sessions
    except Exception as e:
        logger.error(f"Error listing idle chat sessions: {e}")
        raise


# -- chat_message -------------------------------------------------------------


async def insert_chat_message(
    session_id: str,
    role: ChatMessageRole,
    content: Optional[str] = None,
) -> Optional[ChatMessage]:
    """Append one message; idx is auto-allocated atomically by the query."""
    query, values = insert_chat_message_query(
        session_id=session_id,
        role=role.value,
        content=content,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_message(row) if row else None
    except Exception as e:
        logger.error(
            f"Error inserting chat message into session {session_id} "
            f"(role={role.value}): {e}"
        )
        raise


async def list_chat_messages_for_session(
    session_id: str, limit: Optional[int] = None
) -> List[ChatMessage]:
    """Return chat messages for a session, oldest-first.

    ``limit=None`` returns the full log (transcript / resume use). Pass
    a positive int to cap to the N most recent messages — used by the
    per-turn LLM-context replay so reads don't grow with conversation
    length.
    """
    query, values = list_chat_messages_for_session_query(session_id, limit=limit)
    try:
        rows = await run_parameterized_query(query, values)
        messages: List[ChatMessage] = []
        for row in rows or []:
            decoded = decode_chat_message(row)
            if decoded:
                messages.append(decoded)
        return messages
    except Exception as e:
        logger.error(f"Error listing chat messages for session {session_id}: {e}")
        raise
