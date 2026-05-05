"""Decoders for chat_session and chat_message rows."""

from typing import Optional

import asyncpg

from app.schemas.breeze_buddy.chat import (
    ChatEndedReason,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
)
from app.utils.common import parse_json


def decode_chat_session(row: asyncpg.Record) -> Optional[ChatSession]:
    """Build a ChatSession from a DB row, or None if row is empty."""
    if not row:
        return None
    metadata = parse_json(row, "metadata") or {}
    ended_reason_raw = row.get("ended_reason")
    return ChatSession(
        id=str(row["id"]),
        template_id=str(row["template_id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row.get("merchant_id"),
        status=ChatSessionStatus(row["status"]),
        outcome=row.get("outcome"),
        current_node=row.get("current_node"),
        metadata=metadata,
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        ended_at=row.get("ended_at"),
        ended_reason=(ChatEndedReason(ended_reason_raw) if ended_reason_raw else None),
    )


def decode_chat_message(row: asyncpg.Record) -> Optional[ChatMessage]:
    """Build a ChatMessage from a DB row, or None if row is empty."""
    if not row:
        return None
    return ChatMessage(
        session_id=str(row["session_id"]),
        idx=row["idx"],
        role=ChatMessageRole(row["role"]),
        content=row.get("content"),
        created_at=row["created_at"],
    )
