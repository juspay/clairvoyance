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
    decode_chat_session_summary,
    decode_chat_turn_metrics,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.chat_session import (
    count_chat_sessions_query,
    create_chat_session_query,
    end_chat_session_query,
    flip_chat_session_channel_query,
    get_agent_session_state_query,
    get_chat_session_by_id_query,
    insert_chat_message_query,
    list_chat_messages_for_session_query,
    list_chat_sessions_query,
    list_chat_turn_metrics_for_session_query,
    list_idle_chat_sessions_query,
    merge_client_context_query,
    record_chat_turn_metrics_query,
    set_chat_session_voice_lead_query,
    update_chat_session_after_turn_query,
    update_chat_session_outcome_query,
    upsert_agent_session_state_merge_query,
    upsert_agent_session_state_query,
)
from app.schemas.breeze_buddy.chat import (
    AgentSessionState,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
    ChatSessionSummary,
    ChatTurnMetrics,
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


async def update_chat_session_outcome(
    session_id: str,
    outcome: str,
) -> Optional[str]:
    """Set the singular outcome on a chat_session WITHOUT ending it.

    Used by the chat-aware ``update_outcome_in_database`` hook so an assist /
    chat session records an outcome as soon as the LLM sets one. Returns the
    written outcome, or None if no row matched. Raises on DB error.
    """
    query, values = update_chat_session_outcome_query(session_id, outcome)
    try:
        result = await run_parameterized_query(query, values)
        if not result:
            return None
        return result[0]["outcome"]
    except Exception as e:
        logger.error(
            f"Error setting outcome '{outcome}' on chat session {session_id}: {e}"
        )
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


# -- conversational-log listing (CHAT_ANALYTICS_PLAN.md, Phase 1A) -----------


async def list_chat_sessions(
    filters: Dict[str, Any], limit: int, offset: int
) -> List[ChatSessionSummary]:
    """Paginated session summaries for the conversational-log rail.

    ``filters`` is the post-RBAC dict (caller-scoped reseller/merchant already
    applied). Returns most-recently-active sessions with derived
    ``message_count`` + truncated ``preview``.
    """
    query, values = list_chat_sessions_query(filters, limit=limit, offset=offset)
    try:
        rows = await run_parameterized_query(query, values)
        summaries: List[ChatSessionSummary] = []
        for row in rows or []:
            decoded = decode_chat_session_summary(row)
            if decoded:
                summaries.append(decoded)
        return summaries
    except Exception as e:
        logger.error(f"Error listing chat sessions: {e}")
        raise


async def count_chat_sessions(filters: Dict[str, Any]) -> int:
    """Total sessions matching the filters (pagination total)."""
    query, values = count_chat_sessions_query(filters)
    try:
        rows = await run_parameterized_query(query, values)
        row = rows[0] if rows else None
        return int(row["total"]) if row else 0
    except Exception as e:
        logger.error(f"Error counting chat sessions: {e}")
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


# -- widget-mode channel state mutations (migration 030) --------------------


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
    session_id: str, voice_lead_id: Optional[str]
) -> Optional[ChatSession]:
    """Flip current_channel VOICE → CHAT. Used as the /voice/end
    rollback path (when end_conversation drain isn't going to happen).

    Conditioned on (current_channel=VOICE AND voice_lead_id matches)
    so a stale callback for an old lead can't clobber a re-attached
    session. voice_lead_id is preserved (the lead is still bound to
    this conversation).

    Pass ``voice_lead_id=None`` for the orphan-state path
    (`current_channel=VOICE` but `voice_lead_id` is NULL) — the query
    builder omits the voice_lead_id predicate when None, so the
    conditional UPDATE still matches the inconsistent row by id +
    current_channel alone.
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


async def upsert_agent_session_state_merge(
    chat_session_id: str,
    patch: Dict[str, Any],
) -> Optional[AgentSessionState]:
    """Shallow top-level MERGE of ``patch`` into the session's data.

    For turn / drain writers: persists only the keys the caller owns
    (reducer-built state, sans the client-context keys) so it never clobbers
    a concurrent ``/context`` push. See ``strip_client_context_keys``.
    """
    query, values = upsert_agent_session_state_merge_query(
        chat_session_id=chat_session_id,
        patch_json=json.dumps(patch),
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_agent_session_state(row) if row else None
    except Exception as e:
        logger.error(f"Error merging agent_session_state for {chat_session_id}: {e}")
        raise


async def merge_client_context(
    chat_session_id: str,
    *,
    state_patch: Dict[str, Any],
    facts_patch: Optional[Dict[str, Any]],
    revision: Optional[int],
    replace_facts: bool,
) -> Optional[AgentSessionState]:
    """Atomically merge a client-context patch (lock-free, clobber-free).

    Top-level state keys overlay; the ``_client_context`` facts namespace is
    deep-merged (or replaced when ``replace_facts``); ``revision`` is the
    monotonic guard. ``facts_patch is None`` => the namespace is left
    untouched (e.g. a state-only ``merge='replace'`` push must not wipe
    facts). Returns the updated row, or ``None`` when a stale revision was
    skipped (caller treats that as ``applied=false``).
    """
    touch_facts = facts_patch is not None
    top_patch: Dict[str, Any] = dict(state_patch)
    if touch_facts:
        # Included so the first-INSERT row is complete; the UPDATE branch
        # recomputes _client_context via jsonb_set (see the query docstring).
        # Literals (not an import) keep the DB layer free of the agent module;
        # source of truth is chat/client_context.py.
        top_patch["_client_context"] = facts_patch
    if revision is not None:
        top_patch["_client_context_rev"] = revision

    query, values = merge_client_context_query(
        chat_session_id=chat_session_id,
        top_patch_json=json.dumps(top_patch),
        facts_patch_json=json.dumps(facts_patch or {}),
        revision=revision,
        replace_facts=replace_facts,
        touch_facts=touch_facts,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_agent_session_state(row) if row else None
    except Exception as e:
        logger.error(f"Error merging client context for {chat_session_id}: {e}")
        raise


# -- chat_turn_metrics (migration 032) --------------------------------------


async def record_chat_turn_metrics(
    session_id: str,
    idx: int,
    *,
    ttft_ms: Optional[float],
    ttfui_ms: Optional[float],
    ttlui_ms: Optional[float],
    total_ms: Optional[float],
    ui_ops: int,
    ui_dropped: int,
    healer_applied: int,
    tool_calls: int,
    prose_chars: int,
    ui_chars: int,
    status: Optional[str],
    phase: str,
    drops: Optional[List[Dict[str, Any]]] = None,
) -> Optional[ChatTurnMetrics]:
    """Upsert one turn's structural metrics, keyed by the assistant idx.

    Mirrors the router's ``[CHAT_METRICS]`` log line into a joinable row,
    plus the per-drop evidence array (``drops``, migration 041). The
    caller (router stream ``finally``) treats this as best-effort — it wraps
    the call so a write failure never affects the turn. Per accessor
    convention this still logs + re-raises; the router swallows.
    """
    query, values = record_chat_turn_metrics_query(
        session_id,
        idx,
        ttft_ms=ttft_ms,
        ttfui_ms=ttfui_ms,
        ttlui_ms=ttlui_ms,
        total_ms=total_ms,
        ui_ops=ui_ops,
        ui_dropped=ui_dropped,
        healer_applied=healer_applied,
        tool_calls=tool_calls,
        prose_chars=prose_chars,
        ui_chars=ui_chars,
        status=status,
        phase=phase,
        drops_json=json.dumps(drops) if drops else None,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_chat_turn_metrics(row) if row else None
    except Exception as e:
        logger.error(
            f"Error recording chat turn metrics (session={session_id}, idx={idx}): {e}"
        )
        raise


async def list_chat_turn_metrics_for_session(
    session_id: str,
) -> List[ChatTurnMetrics]:
    """All turn metrics for a session, ordered by idx — the transcript join."""
    query, values = list_chat_turn_metrics_for_session_query(session_id)
    try:
        rows = await run_parameterized_query(query, values)
        metrics: List[ChatTurnMetrics] = []
        for row in rows or []:
            decoded = decode_chat_turn_metrics(row)
            if decoded:
                metrics.append(decoded)
        return metrics
    except Exception as e:
        logger.error(f"Error listing chat turn metrics for session {session_id}: {e}")
        raise
