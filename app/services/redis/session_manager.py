"""Redis-based session and conversation management service."""

import asyncio
import json
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import redis.asyncio as redis
from app.core import config
from app.core.logger import logger


class RedisSessionManager:
    """
    Redis-based service for managing user sessions and conversation messages.
    """

    def __init__(self):
        self._redis_client: Optional[redis.Redis] = None
        self._connection_pool: Optional[redis.ConnectionPool] = None
        self._enabled = config.ENABLE_REDIS_SESSION_TRACKING
        
        if self._enabled:
            self._initialize_redis()

    def _initialize_redis(self):
        """Initialize Redis connection pool."""
        try:
            # Create connection pool for better performance
            self._connection_pool = redis.ConnectionPool(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD if config.REDIS_PASSWORD else None,
                db=config.REDIS_DB,
                decode_responses=True,
                max_connections=20,
                retry_on_timeout=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            
            self._redis_client = redis.Redis(connection_pool=self._connection_pool)
            logger.info(f"Redis session manager initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize Redis connection: {e}")
            self._enabled = False
            self._redis_client = None

    async def _ensure_connection(self) -> bool:
        """Ensure Redis connection is available and working."""
        if not self._enabled or not self._redis_client:
            return False
            
        try:
            await self._redis_client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis connection check failed: {e}")
            return False

    def _get_user_sessions_key(self, user_email: str) -> str:
        """Get Redis key for user's session set."""
        return f"{config.REDIS_SESSION_KEY_PREFIX}:{user_email}"

    def _get_conversation_key(self, session_id: str, user_email: str) -> str:
        """Get Redis key for session's conversation object."""
        return f"{config.REDIS_CONVERSATION_KEY_PREFIX}:{user_email}:{session_id}"

    async def add_user_session(self, user_email: str, session_id: str) -> bool:
        """
        Add session_id to user's session set with TTL.
        
        Args:
            user_email: User's email address
            session_id: Session identifier
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not await self._ensure_connection():
            logger.warning("Redis not available, skipping session tracking")
            return False

        try:
            key = self._get_user_sessions_key(user_email)
            
            # Use pipeline for atomic operations
            async with self._redis_client.pipeline() as pipe:
                # Add session to user's set
                pipe.sadd(key, session_id)
                # Set TTL on the key
                pipe.expire(key, config.REDIS_SESSION_TTL)
                await pipe.execute()
            
            logger.info(f"Added session {session_id} to Redis for user {user_email}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add user session to Redis: {e}")
            return False

    async def get_user_sessions(self, user_email: str) -> List[str]:
        """
        Get all session IDs for a user.
        
        Args:
            user_email: User's email address
            
        Returns:
            List[str]: List of session IDs
        """
        if not await self._ensure_connection():
            return []

        try:
            key = self._get_user_sessions_key(user_email)
            sessions = await self._redis_client.smembers(key)
            
            # Convert set to list and ensure string type
            session_list = list(sessions) if sessions else []
            logger.debug(f"Retrieved {len(session_list)} sessions for user {user_email}")
            
            return session_list
            
        except Exception as e:
            logger.error(f"Failed to get user sessions from Redis: {e}")
            return []

    async def add_message(self, session_id: str, message: Dict[str, Any], user_email: str = None) -> bool:
        """
        Add a message to the session's conversation JSON object.
        
        Args:
            session_id: Session identifier
            message: Message dict with role and content
            user_email: User email for conversation metadata
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not await self._ensure_connection():
            logger.warning("Redis not available, skipping message storage")
            return False

        try:          
            key = self._get_conversation_key(session_id, user_email or "unknown")
            current_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            
            # Get existing conversation or create new one
            existing_conversation_json = await self._redis_client.get(key)
            
            if existing_conversation_json:
                # Parse existing conversation
                conversation = json.loads(existing_conversation_json)
            else:
                # Create new conversation object
                conversation = {
                    "id": str(uuid.uuid4()),
                    "title": "New Conversation",
                    "sessionId": session_id,
                    "userId": user_email or "unknown",
                    "createdAt": current_time,
                    "updatedAt": current_time,
                    "messages": []
                }
            
            # Generate message ID (msg_1, msg_2, etc.)
            message_count = len(conversation["messages"]) + 1
            message_id = f"msg_{message_count}"
            
            # Create formatted message
            formatted_message = {
                "id": message_id,
                "timestamp": current_time,
                "role": message["role"],
                "content": message.get("content", "")
            }
            
            # Add message to conversation
            conversation["messages"].append(formatted_message)
            conversation["updatedAt"] = current_time
            
            # Save updated conversation to Redis
            conversation_json = json.dumps(conversation)
            await self._redis_client.setex(key, config.REDIS_SESSION_TTL, conversation_json)
            
            logger.debug(f"Added message {message_id} to Redis conversation for session {session_id}: {message.get('role')}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add message to Redis conversation: {e}")
            return False


    async def close(self):
        """Close Redis connections gracefully."""
        if self._redis_client:
            try:
                await self._redis_client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")
        
        if self._connection_pool:
            try:
                await self._connection_pool.disconnect()
                logger.info("Redis connection pool closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection pool: {e}")

    def is_enabled(self) -> bool:
        """Check if Redis session tracking is enabled and available."""
        return self._enabled and self._redis_client is not None
