"""Database accessors for platform-agnostic Human Assist."""

import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.core.logger import logger
from app.database.decoder.breeze_buddy.chat_session import decode_chat_message
from app.database.decoder.breeze_buddy.human_assist import (
    decode_human_assist_conversation,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.human_assist import (
    claim_human_assist_conversation_query,
    close_human_assist_conversation_query,
    create_human_assist_conversation_query,
    get_active_human_assist_for_session_query,
    get_human_assist_conversation_query,
    get_human_assist_scope_signature_query,
    insert_human_assist_platform_message_query,
    list_due_human_assist_query,
    list_human_assist_conversations_query,
    list_human_assist_transcript_query,
    merge_human_assist_metadata_query,
    rollover_human_assist_session_query,
    touch_human_assist_activity_query,
    touch_human_assist_customer_query,
)
from app.schemas.breeze_buddy.chat import ChatMessage
from app.schemas.breeze_buddy.human_assist import HumanAssistConversation


async def _one(
    query: str, values: List[Any], *, context: str
) -> Optional[HumanAssistConversation]:
    try:
        rows = await run_parameterized_query(query, values)
        return decode_human_assist_conversation(rows[0]) if rows else None
    except Exception as e:
        logger.opt(exception=e).error(f"Error {context}")
        raise


async def create_human_assist_conversation(
    *,
    chat_session_id: str,
    claim_timeout_seconds: int,
    metadata: Dict[str, Any],
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    sender_type: str,
) -> Optional[HumanAssistConversation]:
    query, values = create_human_assist_conversation_query(
        chat_session_id=chat_session_id,
        claim_timeout_seconds=claim_timeout_seconds,
        metadata=metadata,
        notification_content=notification_content,
        notification_blocks=notification_blocks,
        sender_type=sender_type,
    )
    return await _one(
        query,
        values,
        context=f"creating human_assist conversation for chat_session {chat_session_id}",
    )


async def rollover_human_assist_session(
    *,
    chat_session_id: str,
    claim_timeout_seconds: int,
    conversation_metadata: Dict[str, Any],
    context_content: str,
    context_blocks: List[Dict[str, Any]],
    context_sender_type: str,
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    notification_sender_type: str,
) -> Optional[HumanAssistConversation]:
    query, values = rollover_human_assist_session_query(
        chat_session_id=chat_session_id,
        claim_timeout_seconds=claim_timeout_seconds,
        conversation_metadata=conversation_metadata,
        context_content=context_content,
        context_blocks=context_blocks,
        context_sender_type=context_sender_type,
        notification_content=notification_content,
        notification_blocks=notification_blocks,
        notification_sender_type=notification_sender_type,
    )
    return await _one(
        query,
        values,
        context=f"rolling over human_assist session for chat_session {chat_session_id}",
    )


async def get_human_assist_conversation(
    conversation_id: str,
    *,
    include_stats: bool = False,
) -> Optional[HumanAssistConversation]:
    query, values = get_human_assist_conversation_query(
        conversation_id,
        include_stats=include_stats,
    )
    return await _one(
        query,
        values,
        context=f"fetching human_assist conversation {conversation_id}",
    )


async def get_active_human_assist_for_session(
    chat_session_id: str,
) -> Optional[HumanAssistConversation]:
    query, values = get_active_human_assist_for_session_query(chat_session_id)
    return await _one(
        query,
        values,
        context=f"fetching active human_assist conversation for chat_session {chat_session_id}",
    )


async def merge_human_assist_metadata(
    conversation_id: str,
    metadata: Dict[str, Any],
) -> Optional[HumanAssistConversation]:
    query, values = merge_human_assist_metadata_query(conversation_id, metadata)
    return await _one(
        query,
        values,
        context=f"merging human_assist metadata for conversation {conversation_id}",
    )


_TALLY_KEYS = (
    "pending_total",
    "open_total",
    "closed_total",
    "timed_out_total",
    "active_total",
)


async def list_human_assist_conversations(
    *,
    statuses: Optional[List[str]],
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    search: Optional[str] = None,
    limit: int,
    offset: int,
) -> Tuple[List[HumanAssistConversation], int, Dict[str, int]]:
    """Return one page plus the tab tallies for the whole scope.

    ``list_human_assist_conversations_query`` always returns exactly one row
    even when the page is empty (filtered out, or paged past the end): its
    ``tallies`` CTE is an ungrouped aggregate (always one row) and its
    ``page`` CTE is joined with ``LEFT JOIN ... ON TRUE``, so ``total`` and
    every tab tally stay correct straight from that single statement.
    """
    scope = (
        f"reseller={reseller_id or reseller_ids} merchant={merchant_id or merchant_ids}"
    )
    query, values = list_human_assist_conversations_query(
        statuses=statuses,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        search=search,
        limit=limit,
        offset=offset,
    )
    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.opt(exception=e).error(
            f"Error listing human_assist conversations for {scope}"
        )
        raise
    conversations = [
        decoded
        for row in rows or []
        if row.get("id") is not None
        if (decoded := decode_human_assist_conversation(row)) is not None
    ]
    counts = {key: int(rows[0][key] or 0) for key in _TALLY_KEYS}
    return conversations, int(rows[0]["total"] or 0), counts


async def list_human_assist_transcript(
    session_id: str,
    after_idx: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[ChatMessage]:
    """Inbox transcript for a ticket, optionally only rows after ``after_idx``."""
    query, values = list_human_assist_transcript_query(session_id, after_idx, limit)
    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.opt(exception=e).error(
            f"Error listing human_assist transcript for session {session_id}"
        )
        raise
    return [
        decoded
        for row in rows or []
        if (decoded := decode_chat_message(row)) is not None
    ]


async def claim_human_assist_conversation(
    conversation_id: str,
    opened_by: str,
    *,
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    sender_type: str,
) -> Optional[HumanAssistConversation]:
    query, values = claim_human_assist_conversation_query(
        conversation_id,
        opened_by,
        notification_content,
        notification_blocks,
        sender_type,
    )
    return await _one(
        query,
        values,
        context=f"claiming human_assist conversation {conversation_id} by {opened_by}",
    )


async def touch_human_assist_customer(
    chat_session_id: str,
    *,
    mark_activity: bool = False,
) -> Optional[HumanAssistConversation]:
    query, values = touch_human_assist_customer_query(
        chat_session_id,
        mark_activity=mark_activity,
    )
    return await _one(
        query,
        values,
        context=f"touching human_assist customer activity for chat_session {chat_session_id}",
    )


async def insert_human_assist_platform_message(
    *,
    session_id: str,
    role: str,
    content: str,
    content_blocks: List[Dict[str, Any]],
    sender_type: Optional[str],
    max_attempts: int = 3,
) -> Optional[ChatMessage]:
    """Insert one provider-normalized message.

    ``idx`` is allocated as ``MAX(idx) + 1`` inside the INSERT, so two
    concurrent inserts for the same session can race and collide on the
    ``(session_id, idx)`` primary key, raising ``UniqueViolationError``.
    Retry a few times here rather than dropping the message.
    """
    query, values = insert_human_assist_platform_message_query(
        session_id=session_id,
        role=role,
        content=content,
        content_blocks=content_blocks,
        sender_type=sender_type,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            rows = await run_parameterized_query(query, values)
            return decode_chat_message(rows[0]) if rows else None
        except asyncpg.UniqueViolationError as e:
            if attempt == max_attempts:
                logger.opt(exception=e).error(
                    f"Error inserting human_assist message for session "
                    f"{session_id}: idx race exhausted {max_attempts} retries"
                )
                raise
            await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
            continue
        except Exception as e:
            logger.opt(exception=e).error(
                f"Error inserting human_assist message for session {session_id}"
            )
            raise


async def touch_human_assist_activity(
    conversation_id: str,
    opened_by: Optional[str] = None,
) -> Optional[HumanAssistConversation]:
    query, values = touch_human_assist_activity_query(conversation_id, opened_by)
    return await _one(
        query,
        values,
        context=f"touching human_assist activity for conversation {conversation_id}",
    )


async def close_human_assist_conversation(
    conversation_id: str,
    *,
    terminal_status: str,
    close_reason: str,
    closed_by: Optional[str],
    allowed_statuses: List[str],
    notification_content: Optional[str],
    notification_blocks: Optional[List[Dict[str, Any]]],
    notification_sender_type: Optional[str],
    end_session: bool = False,
) -> Optional[HumanAssistConversation]:
    query, values = close_human_assist_conversation_query(
        conversation_id,
        terminal_status=terminal_status,
        close_reason=close_reason,
        closed_by=closed_by,
        allowed_statuses=allowed_statuses,
        notification_content=notification_content,
        notification_blocks=notification_blocks,
        notification_sender_type=notification_sender_type,
        end_session=end_session,
    )
    return await _one(
        query,
        values,
        context=f"closing human_assist conversation {conversation_id} ({close_reason})",
    )


async def get_human_assist_scope_signature(
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> Tuple[Optional[datetime], int]:
    query, values = get_human_assist_scope_signature_query(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
    )
    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.opt(exception=e).error(
            f"Error fetching human_assist scope signature for "
            f"reseller={reseller_id or reseller_ids} "
            f"merchant={merchant_id or merchant_ids}"
        )
        raise
    if not rows:
        return None, 0
    return rows[0].get("latest_activity_at"), int(rows[0].get("ticket_count") or 0)


async def list_due_human_assist_claims(
    cutoff: datetime,
    limit: int = 100,
    *,
    exclude_ids: Optional[List[str]] = None,
) -> List[HumanAssistConversation]:
    query, values = list_due_human_assist_query(
        claim_deadline_before=cutoff,
        limit=limit,
        exclude_ids=exclude_ids,
    )
    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.opt(exception=e).error(
            f"Error listing due human_assist claims before {cutoff}"
        )
        raise
    return [
        decoded
        for row in rows or []
        if (decoded := decode_human_assist_conversation(row)) is not None
    ]


async def list_stale_human_assist_customers(
    cutoff: datetime,
    limit: int = 100,
    *,
    exclude_ids: Optional[List[str]] = None,
) -> List[HumanAssistConversation]:
    query, values = list_due_human_assist_query(
        customer_seen_before=cutoff,
        limit=limit,
        exclude_ids=exclude_ids,
    )
    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.opt(exception=e).error(
            f"Error listing stale human_assist customers before {cutoff}"
        )
        raise
    return [
        decoded
        for row in rows or []
        if (decoded := decode_human_assist_conversation(row)) is not None
    ]


__all__ = [
    "claim_human_assist_conversation",
    "close_human_assist_conversation",
    "create_human_assist_conversation",
    "get_active_human_assist_for_session",
    "get_human_assist_conversation",
    "get_human_assist_scope_signature",
    "insert_human_assist_platform_message",
    "list_due_human_assist_claims",
    "list_human_assist_conversations",
    "list_human_assist_transcript",
    "list_stale_human_assist_customers",
    "merge_human_assist_metadata",
    "rollover_human_assist_session",
    "touch_human_assist_activity",
    "touch_human_assist_customer",
]
