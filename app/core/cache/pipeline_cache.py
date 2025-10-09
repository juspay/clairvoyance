"""
Pipeline Cache Manager

Manages cached pipelines with automatic cleanup and session isolation.
"""

import asyncio
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from app.core.config import MAX_CACHED_CONVERSATIONS
from app.core.logger import logger

from .redis_client import get_redis_client


class PipelineCacheManager:
    """
    Manages pipeline caching following senior's optimized approach:
    - Cache running pipelines by session_id
    - Automatic cleanup of idle pipelines
    - Redis for conversation history persistence
    """

    def __init__(self, cleanup_interval: int = 300, max_idle_time: int = 1800):
        self.pipeline_cache: Dict[str, Tuple[any, any, any, float]] = (
            {}
        )  # session_id -> (task, response_collector, context, last_accessed)
        self.conversation_history: OrderedDict[str, list] = (
            OrderedDict()
        )  # In-memory for active sessions, now LRU
        self.cleanup_interval = cleanup_interval  # 5 minutes
        self.max_idle_time = max_idle_time  # 30 minutes
        self._cleanup_task = None
        self._redis = get_redis_client()

    async def start_cleanup_task(self):
        """Start background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Pipeline cleanup task started")

    async def _cleanup_loop(self):
        """Background task to cleanup idle pipelines."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_idle_pipelines()
            except asyncio.CancelledError:
                logger.info("Pipeline cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in pipeline cleanup: {e}")

    async def _cleanup_idle_pipelines(self):
        """Remove pipelines that have been idle too long."""
        current_time = time.time()
        idle_sessions = []

        for session_id, (_, _, _, last_accessed, _) in self.pipeline_cache.items():
            if current_time - last_accessed > self.max_idle_time:
                idle_sessions.append(session_id)

        for session_id in idle_sessions:
            logger.info(f"Cleaning up idle pipeline for session {session_id}")
            await self._cleanup_session_pipeline(session_id)

        if idle_sessions:
            logger.info(f"Cleaned up {len(idle_sessions)} idle pipelines")

    async def _cleanup_session_pipeline(self, session_id: str):
        """Cleanup pipeline and save conversation to Redis."""
        if session_id in self.pipeline_cache:
            cached_data = self.pipeline_cache.pop(session_id)
            # Handle both old and new formats gracefully

            # Save conversation history to Redis before cleanup
            if session_id in self.conversation_history:
                history = self.conversation_history.pop(session_id)
                await self._save_conversation_to_redis(session_id, history)

            # Note: In production, you might want to properly stop the task
            # For now, we just remove it from cache and let it cleanup naturally

    async def get_cached_pipeline(
        self, session_id: str
    ) -> Optional[Tuple[any, any, any, any]]:
        """Get cached pipeline if it exists and is still valid."""
        if session_id in self.pipeline_cache:
            cached_data = self.pipeline_cache[session_id]
            if len(cached_data) == 5:  # New format with processor
                task, response_collector, context, _, text_processor = cached_data
                # Update last accessed time
                self.pipeline_cache[session_id] = (
                    task,
                    response_collector,
                    context,
                    time.time(),
                    text_processor,
                )
                logger.info(f"Reusing cached pipeline for session {session_id}")
                return task, response_collector, context, text_processor
            else:  # Old format without processor
                task, response_collector, context, _ = cached_data
                self.pipeline_cache[session_id] = (
                    task,
                    response_collector,
                    context,
                    time.time(),
                    None,
                )
                logger.info(
                    f"Reusing cached pipeline for session {session_id} (legacy format)"
                )
                return task, response_collector, context, None
        return None

    async def cache_pipeline(
        self,
        session_id: str,
        task: any,
        response_collector: any,
        context: any,
        text_processor: any = None,
    ):
        """Cache a pipeline for future reuse with optional processor reference."""
        self.pipeline_cache[session_id] = (
            task,
            response_collector,
            context,
            time.time(),
            text_processor,
        )
        logger.info(f"Cached pipeline for session {session_id}")

    async def load_conversation_history(self, session_id: str) -> list:
        """Load conversation history from memory or Redis."""
        # First check in-memory cache
        if session_id in self.conversation_history:
            self.conversation_history.move_to_end(session_id)
            logger.info(f"Conversation {session_id} moved to end of in-memory cache")
            return self.conversation_history[session_id]

        # Load from Redis
        history = await self._load_conversation_from_redis(session_id)
        if history:
            self.conversation_history[session_id] = history
            logger.info(
                f"Loaded conversation history from Redis for session {session_id}: {len(history)} messages"
            )
        else:
            self.conversation_history[session_id] = []
            logger.info(f"No conversation history found for session {session_id}")

        return self.conversation_history[session_id]

    async def update_conversation_history(self, session_id: str, messages: list):
        """Update conversation history in memory."""
        # self.conversation_history[session_id] = messages
        self._set_conversation_history(session_id, messages)
        logger.info(
            f"Updated conversation history for session {session_id}: {len(messages)} messages"
        )

    def _set_conversation_history(self, session_id: str, messages: list):
        """Insert/Update session history and enforce LRU size limit."""
        self.conversation_history[session_id] = messages
        # Mark as most recently used
        self.conversation_history.move_to_end(session_id)

        # Enforce LRU size limit
        if len(self.conversation_history) > MAX_CACHED_CONVERSATIONS:
            oldest_session_id, oldest_history = self.conversation_history.popitem(
                last=False
            )
            # Save evicted session history to Redis
            # asyncio.create_task(self._save_conversation_to_redis(oldest_session_id, oldest_history))
            logger.info(
                f"Evicted oldest conversation {oldest_session_id} from memory (LRU)"
            )

    async def _save_conversation_to_redis(self, session_id: str, history: list):
        """Save conversation history to Redis."""
        try:
            key = f"text_conversation:{session_id}"
            await self._redis.set(key, history, ex=7200)  # 2 hours TTL
            logger.info(f"Saved conversation history to Redis for session {session_id}")
        except Exception as e:
            logger.error(
                f"Failed to save conversation to Redis for session {session_id}: {e}"
            )

    async def _load_conversation_from_redis(self, session_id: str) -> Optional[list]:
        """Load conversation history from Redis."""
        try:
            key = f"text_conversation:{session_id}"
            history = await self._redis.get(key)
            return history if history else []
        except Exception as e:
            logger.error(
                f"Failed to load conversation from Redis for session {session_id}: {e}"
            )
            return []

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        current_time = time.time()
        active_count = len(self.pipeline_cache)
        idle_count = sum(
            1
            for _, _, _, last_accessed, _ in self.pipeline_cache.values()
            if current_time - last_accessed > 300  # 5 minutes
        )

        return {
            "active_pipelines": active_count,
            "idle_pipelines": idle_count,
            "total_conversations": len(self.conversation_history),
            "cleanup_interval": self.cleanup_interval,
            "max_idle_time": self.max_idle_time,
        }

    async def cleanup_session(self, session_id: str):
        """Manually cleanup a specific session."""
        await self._cleanup_session_pipeline(session_id)

    async def clear_conversation_history(self, session_id: str):
        """Clear conversation history for a specific session."""
        if session_id in self.conversation_history:
            self.conversation_history.pop(session_id)
            logger.info(
                f"Cleared in-memory conversation history for session {session_id}"
            )

        # Also clear from Redis
        try:
            key = f"text_conversation:{session_id}"
            await self._redis.delete(key)
            logger.info(f"Cleared Redis conversation history for session {session_id}")
        except Exception as e:
            logger.error(
                f"Failed to clear Redis conversation history for session {session_id}: {e}"
            )

    async def shutdown(self):
        """Shutdown cache manager and cleanup resources."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Save all conversation histories to Redis
        for session_id, history in self.conversation_history.items():
            await self._save_conversation_to_redis(session_id, history)

        logger.info("Pipeline cache manager shutdown complete")
