"""crm_conversation / crm_conversation_message / crm_handoff rows -> shapes."""

from typing import Any, Mapping, Optional

from app.crm.conversations.schemas import Handoff, Thread, TimelineRow
from app.crm.shared.decode import jsonb_list, jsonb_object, uuid_or_none


def _id(value: Any) -> str:
    return str(value)


def decode_thread(row: Mapping[str, Any]) -> Thread:
    return Thread(
        id=_id(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        contact_key=row["contact_key"],
        customer_id=uuid_or_none(row["customer_id"]),
        address=row["address"],
        binding_id=uuid_or_none(row["binding_id"]),
        resolved_at=row["resolved_at"],
        assignee_user_id=row["assignee_user_id"],
        bot_template_id=uuid_or_none(row["bot_template_id"]),
        bot_session_id=uuid_or_none(row["bot_session_id"]),
        bot_cursor_at=row["bot_cursor_at"],
        last_inbound_at=row["last_inbound_at"],
        last_message_at=row["last_message_at"],
        preview=row["preview"],
        unread=row["unread"],
        assignment_trail=[
            entry
            for entry in jsonb_list(row["assignment_trail"])
            if isinstance(entry, dict)
        ],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_handoff(row: Mapping[str, Any]) -> Handoff:
    return Handoff(
        id=_id(row["id"]),
        merchant_id=row["merchant_id"],
        conversation_id=_id(row["conversation_id"]),
        chat_session_id=_id(row["chat_session_id"]),
        reason=row["reason"],
        summary=row["summary"],
        priority=row["priority"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        outcome=row["outcome"],
        closed_by=row["closed_by"],
        closed_at=row["closed_at"],
        opened_at=row["opened_at"],
    )


def decode_timeline(row: Mapping[str, Any]) -> TimelineRow:
    body: Optional[dict] = (
        jsonb_object(row["body"]) if row["body"] is not None else None
    )
    return TimelineRow(
        id=_id(row["id"]),
        conversation_id=_id(row["conversation_id"]),
        kind=row["kind"],
        author_kind=row["author_kind"],
        author_user_id=row["author_user_id"],
        event_raw_id=uuid_or_none(row["event_raw_id"]),
        message_id=uuid_or_none(row["message_id"]),
        provider_message_id=row["provider_message_id"],
        body=body,
        occurred_at=row["occurred_at"],
        created_at=row["created_at"],
    )
