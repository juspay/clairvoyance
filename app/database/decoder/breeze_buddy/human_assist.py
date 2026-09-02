"""Row decoders for platform-agnostic Human Assist."""

from typing import Optional

import asyncpg

from app.schemas.breeze_buddy.human_assist import (
    HumanAssistCloseReason,
    HumanAssistConversation,
    HumanAssistStatus,
)
from app.utils.common import parse_json


def decode_human_assist_conversation(
    row: asyncpg.Record,
) -> Optional[HumanAssistConversation]:
    if not row:
        return None
    widget_config_id = row.get("widget_config_id")
    if widget_config_id is None:
        return None
    try:
        status = HumanAssistStatus(row["status"])
    except ValueError:
        return None
    requested_at = row.get("requested_at")
    claim_deadline_at = row.get("claim_deadline_at")
    customer_last_seen_at = row.get("customer_last_seen_at")
    last_activity_at = row.get("last_activity_at")
    if (
        requested_at is None
        or claim_deadline_at is None
        or customer_last_seen_at is None
        or last_activity_at is None
    ):
        return None
    close_reason = row.get("close_reason")
    try:
        decoded_close_reason = (
            HumanAssistCloseReason(close_reason) if close_reason else None
        )
    except ValueError:
        decoded_close_reason = None
    metadata = parse_json(row, "metadata") or {}
    preview = row.get("preview")
    if isinstance(preview, str) and len(preview) > 160:
        preview = preview[:160].rstrip() + "…"
    return HumanAssistConversation(
        id=str(row["id"]),
        chat_session_id=str(row["chat_session_id"]),
        widget_config_id=str(widget_config_id),
        reseller_id=row["reseller_id"],
        merchant_id=row.get("merchant_id"),
        platform=str(metadata.get("platform") or "native"),
        status=status,
        requested_at=requested_at,
        claim_deadline_at=claim_deadline_at,
        opened_at=row.get("opened_at"),
        opened_by=row.get("opened_by"),
        closed_at=row.get("closed_at"),
        closed_by=row.get("closed_by"),
        close_reason=decoded_close_reason,
        last_activity_at=last_activity_at,
        customer_last_seen_at=customer_last_seen_at,
        metadata=metadata,
        message_count=row.get("message_count") or 0,
        preview=preview,
    )


__all__ = ["decode_human_assist_conversation"]
