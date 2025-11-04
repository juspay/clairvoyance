"""
Database accessor functions for conversations.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.conversation.conversations import (
    decode_conversation,
    decode_conversation_message,
)
from app.database.queries import run_parameterized_query
from app.database.queries.conversation.conversations import (
    complete_conversation_query,
    get_conversation_by_session_id_query,
    insert_conversation_query,
    insert_message_query,
    update_conversation_activity_query,
    update_conversation_message_count_query,
)
from app.schemas import Conversation, ConversationMessage


# Conversation accessors
async def create_conversation(
    session_id: str,
    merchant_id: Optional[str] = None,
    client_sid: Optional[str] = None,
    user_email: Optional[str] = None,
    user_name: Optional[str] = None,
    shop_id: Optional[str] = None,
    shop_url: Optional[str] = None,
    reseller_id: Optional[str] = None,
    mode: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Conversation]:
    """
    Create a new conversation record.
    """
    logger.info(f"Creating conversation with session_id: {session_id}")

    try:
        query_text, values = insert_conversation_query(
            session_id=session_id,
            merchant_id=merchant_id,
            client_sid=client_sid,
            user_email=user_email,
            user_name=user_name,
            shop_id=shop_id,
            shop_url=shop_url,
            reseller_id=reseller_id,
            mode=mode,
            metadata=metadata,
        )

        result = await run_parameterized_query(query_text, values)
        if result:
            decoded_result = decode_conversation(result)
            logger.info(f"Conversation created successfully: {session_id}")
            return decoded_result

        logger.error("Failed to create conversation")
        return None

    except Exception as e:
        logger.error(f"Error creating conversation: {e}")
        return None


async def get_conversation_by_session_id(
    session_id: str,
) -> Optional[Conversation]:
    """
    Get conversation by session_id.
    """
    try:
        query_text, values = get_conversation_by_session_id_query(session_id)
        result = await run_parameterized_query(query_text, values)

        if result:
            return decode_conversation(result)

        return None

    except Exception as e:
        logger.error(f"Error getting conversation by session_id: {e}")
        return None


async def update_conversation_activity(
    session_id: str,
) -> Optional[Conversation]:
    """
    Update conversation last activity time.
    """
    try:
        query_text, values = update_conversation_activity_query(session_id)
        result = await run_parameterized_query(query_text, values)

        if result:
            return decode_conversation(result)

        return None

    except Exception as e:
        logger.error(f"Error updating conversation activity: {e}")
        return None


async def complete_conversation(
    session_id: str,
) -> Optional[Conversation]:
    """
    Mark conversation as completed.
    """
    try:
        query_text, values = complete_conversation_query(session_id)
        result = await run_parameterized_query(query_text, values)

        if result:
            logger.info(f"Conversation completed: {session_id}")
            return decode_conversation(result)

        return None

    except Exception as e:
        logger.error(f"Error completing conversation: {e}")
        return None


# Message accessors
async def save_message(
    conversation_id: str,
    role: str,
    content: str,
    sequence_number: int,
) -> Optional[ConversationMessage]:
    """
    Save a conversation message.
    """
    try:
        query_text, values = insert_message_query(
            conversation_id=conversation_id,
            role=role,
            content=content,
            sequence_number=sequence_number,
        )

        result = await run_parameterized_query(query_text, values)
        if result:
            decoded_result = decode_conversation_message(result)

            # Update message count
            await update_message_count(conversation_id)

            return decoded_result

        return None

    except Exception as e:
        logger.error(f"Error saving message: {e}")
        return None


async def update_message_count(conversation_id: str) -> Optional[Conversation]:
    """
    Update message count in conversation.
    """
    try:
        query_text, values = update_conversation_message_count_query(conversation_id)
        result = await run_parameterized_query(query_text, values)

        if result:
            return decode_conversation(result)

        return None

    except Exception as e:
        logger.error(f"Error updating message count: {e}")
        return None
