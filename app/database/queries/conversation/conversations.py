"""
Database query functions for conversations.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Table names
CONVERSATIONS_TABLE = "conversations"
CONVERSATION_MESSAGES_TABLE = "conversation_messages"


# Conversation queries
def insert_conversation_query(
    session_id: str,
    merchant_id: str,
    client_sid: Optional[str] = None,
    user_email: Optional[str] = None,
    user_name: Optional[str] = None,
    shop_id: Optional[str] = None,
    shop_url: Optional[str] = None,
    reseller_id: Optional[str] = None,
    mode: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert conversation record.
    """
    text = f"""
        INSERT INTO "{CONVERSATIONS_TABLE}"
        (
            "session_id",
            "client_sid",
            "merchant_id",
            "user_email",
            "user_name",
            "shop_id",
            "shop_url",
            "reseller_id",
            "mode",
            "status",
            "metadata",
            "started_at",
            "last_activity_at",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) RETURNING *;
    """

    now = datetime.now()

    # Convert metadata dict to JSON string for PostgreSQL JSONB
    metadata_json = json.dumps(metadata) if metadata else None

    values = [
        session_id,
        client_sid,
        merchant_id,
        user_email,
        user_name,
        shop_id,
        shop_url,
        reseller_id,
        mode,
        "active",  # Default status
        metadata_json,
        now,
        now,
        now,
        now,
    ]

    return text, values


def get_conversation_by_session_id_query(session_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get conversation by session_id.
    """
    text = f'SELECT * FROM "{CONVERSATIONS_TABLE}" WHERE "session_id" = $1;'
    values = [session_id]
    return text, values


def update_conversation_activity_query(session_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to update conversation last activity time.
    """
    text = f"""
        UPDATE "{CONVERSATIONS_TABLE}"
        SET "last_activity_at" = NOW(), "updated_at" = NOW()
        WHERE "session_id" = $1
        RETURNING *;
    """
    values = [session_id]
    return text, values


def complete_conversation_query(session_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to mark conversation as completed.
    """
    text = f"""
        UPDATE "{CONVERSATIONS_TABLE}"
        SET "status" = $2, "completed_at" = NOW(), "updated_at" = NOW()
        WHERE "session_id" = $1
        RETURNING *;
    """
    values = [session_id, "completed"]
    return text, values


# Message queries
def insert_message_query(
    conversation_id: str,
    role: str,
    content: str,
    sequence_number: int,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert a message.
    """
    text = f"""
        INSERT INTO "{CONVERSATION_MESSAGES_TABLE}"
        (
            "conversation_id",
            "role",
            "content",
            "sequence_number",
            "timestamp",
            "created_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *;
    """

    now = datetime.now()
    values = [
        conversation_id,
        role,
        content,
        sequence_number,
        now,
        now,
    ]

    return text, values


def update_conversation_message_count_query(
    conversation_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update message count in conversation.
    """
    text = f"""
        UPDATE "{CONVERSATIONS_TABLE}"
        SET "message_count" = (
            SELECT COUNT(*) FROM "{CONVERSATION_MESSAGES_TABLE}"
            WHERE "conversation_id" = $1
        ),
        "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [conversation_id]
    return text, values
