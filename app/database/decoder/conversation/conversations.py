"""
Decoder functions for conversations.
"""

import json
from typing import List, Optional

import asyncpg

from app.schemas import Conversation, ConversationMessage, ConversationStatus


def decode_conversation(result: List[asyncpg.Record]) -> Optional[Conversation]:
    """
    Decode conversation from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None

    row = result[0]

    # Parse metadata JSON string back to dict
    metadata = row["metadata"]
    if metadata and isinstance(metadata, str):
        metadata = json.loads(metadata)

    return Conversation(
        id=str(row["id"]),
        session_id=row["session_id"],
        client_sid=row["client_sid"],
        merchant_id=row["merchant_id"],
        user_email=row["user_email"],
        user_name=row["user_name"],
        shop_id=row["shop_id"],
        shop_url=row["shop_url"],
        reseller_id=row["reseller_id"],
        mode=row["mode"],
        status=ConversationStatus(row["status"]),
        summary=row["summary"],
        message_count=row["message_count"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        completed_at=row["completed_at"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_conversation_message(
    result: List[asyncpg.Record],
) -> Optional[ConversationMessage]:
    """
    Decode conversation message from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None

    row = result[0]
    return ConversationMessage(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role=row["role"],
        content=row["content"],
        sequence_number=row["sequence_number"],
        timestamp=row["timestamp"],
        created_at=row["created_at"],
    )
