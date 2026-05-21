"""Decoders for chat_session, chat_message, and agent_session_state rows."""

from typing import Any, Dict, List, Optional, cast

import asyncpg

from app.schemas.breeze_buddy.chat import (
    AgentSessionState,
    ChatEndedReason,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
    WidgetChannel,
)
from app.utils.common import parse_json


def decode_chat_session(row: asyncpg.Record) -> Optional[ChatSession]:
    """Build a ChatSession from a DB row, or None if row is empty."""
    if not row:
        return None
    metadata = parse_json(row, "metadata") or {}
    ended_reason_raw = row.get("ended_reason")
    # current_channel + voice_lead_id arrived in migration 030. Older
    # SELECTs that don't list them would return None — default to CHAT
    # so the field stays valid for legacy callers.
    channel_raw = row.get("current_channel") or WidgetChannel.CHAT.value
    voice_lead_id_raw = row.get("voice_lead_id")
    return ChatSession(
        id=str(row["id"]),
        template_id=str(row["template_id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row.get("merchant_id"),
        status=ChatSessionStatus(row["status"]),
        outcome=row.get("outcome"),
        current_node=row.get("current_node"),
        metadata=metadata,
        current_channel=WidgetChannel(channel_raw),
        voice_lead_id=str(voice_lead_id_raw) if voice_lead_id_raw else None,
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        ended_at=row.get("ended_at"),
        ended_reason=(ChatEndedReason(ended_reason_raw) if ended_reason_raw else None),
    )


def decode_chat_message(row: asyncpg.Record) -> Optional[ChatMessage]:
    """Build a ChatMessage from a DB row, or None if row is empty."""
    if not row:
        return None
    # parse_json is annotated `Optional[Dict[str, Any]]` for legacy callers
    # but the JSON columns we read here are stored as arrays (lists of block
    # dicts). The runtime accepts whatever shape the column held; the cast
    # silences pyrefly's narrow return-type inference without changing the
    # shared helper's signature (which would ripple through many callers).
    blocks_raw = cast(Optional[List[Dict[str, Any]]], parse_json(row, "content_blocks"))
    ui_raw = cast(Optional[List[Dict[str, Any]]], parse_json(row, "ui_blocks"))
    return ChatMessage(
        session_id=str(row["session_id"]),
        idx=row["idx"],
        role=ChatMessageRole(row["role"]),
        content=row.get("content"),
        content_blocks=blocks_raw,
        ui_blocks=ui_raw,
        created_at=row["created_at"],
    )


def decode_agent_session_state(row: asyncpg.Record) -> Optional[AgentSessionState]:
    """Build an AgentSessionState from a DB row, or None if row is empty."""
    if not row:
        return None
    return AgentSessionState(
        chat_session_id=str(row["chat_session_id"]),
        data=parse_json(row, "data") or {},
        updated_at=row["updated_at"],
    )
