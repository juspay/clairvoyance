"""Async accessors for chat_session, chat_message, and agent_session_state.

Calls into queries/breeze_buddy/chat_session.py for SQL and
decoder/breeze_buddy/chat_session.py for row → Pydantic.
JSON serialization for JSONB columns happens here.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.chat_session import (
    decode_agent_session_state,
    decode_chat_message,
    decode_chat_session,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.chat_session import (
    create_chat_session_query,
    drain_voice_into_chat_session_query,
    end_chat_session_query,
    flip_chat_session_channel_query,
    get_agent_session_state_query,
    get_chat_session_by_id_query,
    insert_chat_message_query,
    list_chat_messages_for_session_query,
    list_idle_chat_sessions_query,
    set_chat_session_voice_lead_query,
    update_chat_session_after_turn_query,
    upsert_agent_session_state_query,
)
from app.schemas.breeze_buddy.chat import (
    AgentSessionState,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
    WidgetChannel,
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
    content_blocks: Optional[List[Dict[str, Any]]] = None,
    ui_blocks: Optional[List[Dict[str, Any]]] = None,
) -> Optional[ChatMessage]:
    """Append one message; idx is auto-allocated atomically by the query.

    ``content_blocks`` is the canonical Anthropic content array (text +
    tool_use on assistant rows, text + tool_result on user rows). When
    supplied it's the source of truth for history replay; ``content``
    remains as a denormalised prose view. Pre-migration callers that
    pass only ``content`` still work — the column defaults to NULL and
    the loader synthesises a [text] block.

    ``ui_blocks`` (migration 030) is the SpecStream ui_op list emitted
    during an assistant turn. Consumed only by the widget resume path
    to repaint Tiles/Carousels — the LLM never sees this column. Pass
    ``None`` (default) on user turns and on assistant turns without UI.
    """
    query, values = insert_chat_message_query(
        session_id=session_id,
        role=role.value,
        content=content,
        content_blocks_json=(
            json.dumps(content_blocks) if content_blocks is not None else None
        ),
        ui_blocks_json=(json.dumps(ui_blocks) if ui_blocks else None),
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


# -- widget-mode channel state mutations (migration 029) --------------------


async def bind_voice_lead_to_chat_session(
    session_id: str, voice_lead_id: str
) -> Optional[ChatSession]:
    """First-time bind of a voice lead to a chat_session.

    Conditioned on ``voice_lead_id IS NULL`` so a concurrent
    /voice/connect can't overwrite an already-bound lead. Returns
    the updated row, or None when the row already had a
    voice_lead_id (caller should reuse the existing lead).
    """
    query, values = set_chat_session_voice_lead_query(session_id, voice_lead_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_session(row) if row else None
    except Exception as e:
        logger.error(
            f"Error binding voice lead {voice_lead_id} to chat_session "
            f"{session_id}: {e}"
        )
        raise


async def flip_chat_session_to_voice(session_id: str) -> Optional[ChatSession]:
    """Flip current_channel CHAT → VOICE.

    Conditioned on the row currently being CHAT. Returns the updated
    row, or None when the precondition failed (409 from caller).
    Used by POST /widget/session/{id}/voice/connect.
    """
    query, values = flip_chat_session_channel_query(
        session_id,
        new_channel=WidgetChannel.VOICE.value,
        expected_channel=WidgetChannel.CHAT.value,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_session(row) if row else None
    except Exception as e:
        logger.error(f"Error flipping chat_session {session_id} to VOICE: {e}")
        raise


async def flip_chat_session_to_chat(
    session_id: str, voice_lead_id: str
) -> Optional[ChatSession]:
    """Flip current_channel VOICE → CHAT. Used as the /voice/end
    rollback path (when end_conversation drain isn't going to happen).

    Conditioned on (current_channel=VOICE AND voice_lead_id matches)
    so a stale callback for an old lead can't clobber a re-attached
    session. voice_lead_id is preserved (the lead is still bound to
    this conversation).
    """
    query, values = flip_chat_session_channel_query(
        session_id,
        new_channel=WidgetChannel.CHAT.value,
        expected_channel=WidgetChannel.VOICE.value,
        expected_voice_lead_id=voice_lead_id,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_session(row) if row else None
    except Exception as e:
        logger.error(f"Error flipping chat_session {session_id} to CHAT: {e}")
        raise


async def drain_voice_into_chat_session(
    *,
    chat_session_id: str,
    lead_id: str,
    new_messages: List[Dict[str, Any]],
    final_node: Optional[str],
) -> bool:
    """End-of-voice drain: append new turns, advance current_node,
    flip channel back to CHAT.

    voice_lead_id is intentionally PRESERVED — the lead is bound to
    the conversation for its full lifetime; the next /voice/connect
    reuses it via attempt_count.

    Idempotent against duplicate fires (e.g., end_conversation invoked
    twice). The (session_id, idx) PK + atomic idx subquery in
    insert_chat_message_query make duplicate INSERTs impossible at
    the DB level; we wrap each insert just in case so a single
    duplicate doesn't stop the rest.

    Returns True when the channel flip succeeded (this drain owned
    the close-out). False when the row had already moved on
    (another caller ended the voice first).
    """
    # 1. Append any new turns.
    for msg in new_messages:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        try:
            await insert_chat_message(
                session_id=chat_session_id,
                role=ChatMessageRole(role),
                content=content,
            )
        except Exception as e:
            logger.warning(
                f"drain: skip insert (session={chat_session_id}, role={role!r}): {e}"
            )

    # 2. Flip channel + advance current_node atomically. voice_lead_id
    # stays bound for reuse on the next /voice/connect.
    query, values = drain_voice_into_chat_session_query(
        chat_session_id,
        final_node=final_node,
        expected_voice_lead_id=lead_id,
    )
    try:
        result = await run_parameterized_query(query, values)
        if result:
            logger.info(
                f"drain: voice lead {lead_id} drained into chat_session "
                f"{chat_session_id}, final_node={final_node!r} "
                "(voice_lead_id preserved for reuse)"
            )
            return True
        logger.info(
            f"drain: chat_session {chat_session_id} no longer matches "
            f"lead {lead_id}; flip skipped"
        )
        return False
    except Exception as e:
        logger.error(
            f"drain: failed to flip chat_session {chat_session_id} after "
            f"lead {lead_id}: {e}"
        )
        raise


# -- agent_session_state ------------------------------------------------------


async def get_agent_session_state(
    chat_session_id: str,
) -> Optional[AgentSessionState]:
    """Read the agent state row for a session. Returns None if absent.

    The runtime treats `data` as opaque — what's inside is determined
    by template-declared reducers.
    """
    query, values = get_agent_session_state_query(chat_session_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_agent_session_state(row) if row else None
    except Exception as e:
        logger.error(f"Error reading agent_session_state for {chat_session_id}: {e}")
        raise


async def upsert_agent_session_state(
    chat_session_id: str,
    data: Dict[str, Any],
) -> Optional[AgentSessionState]:
    """INSERT-or-REPLACE the full data dict for a session.

    Caller is responsible for computing the merged next state via the
    reducer engine. This accessor is a dumb store — keeps merging logic
    pure-Python and easy to test.
    """
    query, values = upsert_agent_session_state_query(
        chat_session_id=chat_session_id,
        data_json=json.dumps(data),
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_agent_session_state(row) if row else None
    except Exception as e:
        logger.error(f"Error upserting agent_session_state for {chat_session_id}: {e}")
        raise
