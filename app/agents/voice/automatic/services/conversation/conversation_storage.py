"""
Conversation Storage Service.
Handles all conversation and message storage operations.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.accessor.conversation.conversations import (
    complete_conversation as db_complete_conversation,
)
from app.database.accessor.conversation.conversations import (
    create_conversation as db_create_conversation,
)
from app.database.accessor.conversation.conversations import (
    get_conversation_by_session_id,
)
from app.database.accessor.conversation.conversations import (
    save_message as db_save_message,
)
from app.database.accessor.conversation.conversations import (
    update_conversation_activity,
)
from app.schemas import Conversation, ConversationMessage


class ConversationStorageService:
    """
    Service for managing conversation storage.
    Provides high-level interface for conversation operations.
    """

    async def create_conversation(
        self,
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
        Create a new conversation.

        Args:
            session_id: Unique session identifier
            merchant_id: Merchant identifier
            client_sid: Optional client session ID
            user_email: User email
            user_name: User name
            shop_id: Shop identifier
            shop_url: Shop URL
            reseller_id: Reseller identifier
            mode: Conversation mode (e.g., 'TEST' or 'LIVE')
            metadata: Additional metadata

        Returns:
            Created Conversation object or None if failed
        """
        conversation = await db_create_conversation(
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

        if conversation:
            logger.info(f"Conversation created: {session_id}")

        return conversation

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        last_saved_user_message: Optional[str] = None,
    ) -> tuple[Optional[ConversationMessage], Optional[str]]:
        """
        Save a message to a conversation.

        Args:
            session_id: Session identifier
            role: Message role ('user' or 'assistant')
            content: Message content
            last_saved_user_message: Last saved user message to prevent duplicates (for user messages only)

        Returns:
            Tuple of (Saved ConversationMessage or None if failed, updated last_saved_user_message)
        """
        # For user messages, check if it's a duplicate
        if role == "user" and last_saved_user_message == content:
            logger.debug(f"Skipping duplicate user message for session: {session_id}")
            return (None, last_saved_user_message)

        # Get conversation
        conversation = await get_conversation_by_session_id(session_id)
        if not conversation:
            logger.error(f"Conversation not found for session: {session_id}")
            return (None, last_saved_user_message)

        # Calculate sequence number (current count + 1)
        sequence_number = conversation.message_count + 1

        # Save message
        message = await db_save_message(
            conversation_id=conversation.id,
            role=role,
            content=content,
            sequence_number=sequence_number,
        )

        if message:
            # Update conversation activity
            await update_conversation_activity(session_id)
        else:
            logger.error(f"Failed to save message to session: {session_id}")

        # Update last_saved_user_message if this was a user message
        new_last_saved = (
            content if role == "user" and message else last_saved_user_message
        )

        return (message, new_last_saved)

    async def complete_conversation(self, session_id: str) -> Optional[Conversation]:
        """
        Mark conversation as completed.

        Args:
            session_id: Session identifier

        Returns:
            Updated Conversation object or None if failed
        """
        conversation = await db_complete_conversation(session_id)

        if conversation:
            logger.info(f"Conversation completed: {session_id}")
        else:
            logger.error(f"Failed to complete conversation: {session_id}")

        return conversation


# Global service instance
_conversation_storage_service: Optional[ConversationStorageService] = None


def get_conversation_storage_service() -> ConversationStorageService:
    """
    Get the global conversation storage service instance.

    Returns:
        ConversationStorageService instance
    """
    global _conversation_storage_service
    if _conversation_storage_service is None:
        _conversation_storage_service = ConversationStorageService()
    return _conversation_storage_service
