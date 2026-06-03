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
    ChatSessionSummary,
    ChatTurnMetrics,
    WidgetChannel,
)
from app.utils.common import parse_json

# Cap the list-rail preview so a long message can't bloat the list payload.
_PREVIEW_MAX_CHARS = 160


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


def decode_chat_session_summary(row: asyncpg.Record) -> Optional[ChatSessionSummary]:
    """Build a ChatSessionSummary (list-rail row) from a DB row.

    The query supplies derived ``message_count`` + ``preview`` alongside the
    session columns. The preview is truncated here so the truncation policy
    stays in one place (the SQL returns the full latest-message content).
    """
    if not row:
        return None
    channel_raw = row.get("current_channel") or WidgetChannel.CHAT.value
    preview = row.get("preview")
    if isinstance(preview, str) and len(preview) > _PREVIEW_MAX_CHARS:
        preview = preview[:_PREVIEW_MAX_CHARS].rstrip() + "…"
    return ChatSessionSummary(
        id=str(row["id"]),
        template_id=str(row["template_id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row.get("merchant_id"),
        status=ChatSessionStatus(row["status"]),
        outcome=row.get("outcome"),
        current_channel=WidgetChannel(channel_raw),
        message_count=row.get("message_count") or 0,
        preview=preview,
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        ended_at=row.get("ended_at"),
    )


def decode_chat_turn_metrics(row: asyncpg.Record) -> Optional[ChatTurnMetrics]:
    """Build a ChatTurnMetrics from a chat_turn_metrics row (migration 032)."""
    if not row:
        return None
    return ChatTurnMetrics(
        session_id=str(row["session_id"]),
        idx=row["idx"],
        ttft_ms=row.get("ttft_ms"),
        ttfui_ms=row.get("ttfui_ms"),
        ttlui_ms=row.get("ttlui_ms"),
        total_ms=row.get("total_ms"),
        ui_ops=row.get("ui_ops") or 0,
        ui_dropped=row.get("ui_dropped") or 0,
        healer_applied=row.get("healer_applied") or 0,
        tool_calls=row.get("tool_calls") or 0,
        prose_chars=row.get("prose_chars") or 0,
        ui_chars=row.get("ui_chars") or 0,
        status=row.get("status"),
        phase=row.get("phase") or "baseline",
        created_at=row["created_at"],
    )
