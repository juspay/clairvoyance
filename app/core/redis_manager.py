import asyncio
import json
from typing import Any, Dict, Optional, List
import redis.asyncio as redis
from app.core.logger import logger
from app.core.config import (
    REDIS_URL, 
    REDIS_SESSION_QUEUE, 
    REDIS_WORKER_RESPONSE_QUEUE,
    REDIS_CACHE_TTL,
    WORKER_REGISTRATION_TTL,
    SESSION_ASSIGNMENT_TTL,
    HEARTBEAT_TTL
)


class RedisManager:
    """Redis connection manager for session queues and caching."""
    
    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self._connection_pool = None
        
    async def connect(self):
        """Initialize Redis connection."""
        try:
            self.redis = redis.from_url(
                REDIS_URL, 
                encoding="utf-8",
                decode_responses=True,
                max_connections=20
            )
            # Test connection
            await self.redis.ping()
            logger.info(f"Connected to Redis at {REDIS_URL}")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def disconnect(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            logger.info("Redis connection closed")
    
    # Session Queue Operations
    async def enqueue_session(self, session_data: Dict[str, Any]) -> bool:
        """Add a session to the processing queue for a specific worker."""
        try:
            worker_id = session_data.get("worker_id")
            if not worker_id:
                logger.error("No worker_id in session data")
                return False
                
            # Use per-worker queue instead of shared queue
            worker_queue = f"worker:{worker_id}:sessions"
            await self.redis.lpush(worker_queue, json.dumps(session_data))
            logger.info(f"Enqueued session {session_data.get('session_id')} to worker queue {worker_queue}")
            return True
        except Exception as e:
            logger.error(f"Failed to enqueue session: {e}")
            return False
    
    async def dequeue_session(self, worker_id: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
        """Get next session from worker-specific queue (blocking)."""
        try:
            worker_queue = f"worker:{worker_id}:sessions"
            result = await self.redis.brpop(worker_queue, timeout=timeout)
            if result:
                _, session_json = result
                session_data = json.loads(session_json)
                logger.info(f"Dequeued session {session_data.get('session_id')} from worker queue {worker_queue}")
                return session_data
            return None
        except Exception as e:
            logger.error(f"Failed to dequeue session for worker {worker_id}: {e}")
            return None
    
    # Worker Response Operations  
    async def send_worker_response(self, response_data: Dict[str, Any]) -> bool:
        """Send response from worker to session manager."""
        try:
            await self.redis.lpush(REDIS_WORKER_RESPONSE_QUEUE, json.dumps(response_data))
            return True
        except Exception as e:
            logger.error(f"Failed to send worker response: {e}")
            return False
    
    async def get_worker_response(self, timeout: int = 1) -> Optional[Dict[str, Any]]:
        """Get worker response (non-blocking with short timeout)."""
        try:
            result = await self.redis.brpop(REDIS_WORKER_RESPONSE_QUEUE, timeout=timeout)
            if result:
                _, response_json = result
                return json.loads(response_json)
            return None
        except Exception as e:
            logger.error(f"Failed to get worker response: {e}")
            return None
    
    # Pub/Sub for real-time communication
    async def publish(self, channel: str, message: Dict[str, Any]) -> bool:
        """Publish message to channel."""
        try:
            await self.redis.publish(channel, json.dumps(message))
            return True
        except Exception as e:
            logger.error(f"Failed to publish to {channel}: {e}")
            return False
    
    async def subscribe(self, channels: List[str]):
        """Subscribe to channels and return pubsub object."""
        try:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(*channels)
            return pubsub
        except Exception as e:
            logger.error(f"Failed to subscribe to {channels}: {e}")
            return None
    
    # Caching Operations
    async def cache_set(self, key: str, value: Any, ttl: int = None) -> bool:
        """Set cache value with TTL."""
        try:
            ttl = ttl or REDIS_CACHE_TTL
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            await self.redis.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.error(f"Failed to set cache {key}: {e}")
            return False
    
    async def cache_get(self, key: str) -> Optional[Any]:
        """Get cache value."""
        try:
            value = await self.redis.get(key)
            if value:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
            return None
        except Exception as e:
            logger.error(f"Failed to get cache {key}: {e}")
            return None
    
    async def cache_delete(self, key: str) -> bool:
        """Delete cache key."""
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Failed to delete cache {key}: {e}")
            return False
    
    # Session State Management
    async def set_session_state(self, session_id: str, state: Dict[str, Any]) -> bool:
        """Store session state."""
        return await self.cache_set(f"session:{session_id}", state, ttl=7200)  # 2 hours
    
    async def get_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session state."""
        return await self.cache_get(f"session:{session_id}")
    
    async def delete_session_state(self, session_id: str) -> bool:
        """Delete session state."""
        return await self.cache_delete(f"session:{session_id}")
    
    # Worker Management
    async def register_worker(self, worker_id: str, worker_info: Dict[str, Any]) -> bool:
        """Register worker with TTL-based auto-expiration."""
        return await self.cache_set(f"worker:{worker_id}", worker_info, ttl=WORKER_REGISTRATION_TTL)
    
    async def get_active_workers(self) -> List[str]:
        """Get list of active worker IDs."""
        try:
            keys = await self.redis.keys("worker:*")
            # Filter out non-registration keys (like worker:id:sessions, worker:id:heartbeat)
            worker_ids = []
            for key in keys:
                key_parts = key.split(":")
                if len(key_parts) == 2:  # Only worker:id format
                    worker_ids.append(key_parts[1])
            return worker_ids
        except Exception as e:
            logger.error(f"Failed to get active workers: {e}")
            return []
    
    async def update_worker_heartbeat(self, worker_id: str) -> bool:
        """Update worker heartbeat with TTL."""
        try:
            heartbeat_key = f"worker:{worker_id}:heartbeat"
            await self.redis.setex(heartbeat_key, HEARTBEAT_TTL, "alive")
            return True
        except Exception as e:
            logger.error(f"Failed to update heartbeat for worker {worker_id}: {e}")
            return False
    
    async def set_session_assignment(self, session_id: str, worker_id: str) -> bool:
        """Track session assignment with TTL."""
        try:
            assignment_key = f"session:{session_id}:assignment"
            await self.redis.setex(assignment_key, SESSION_ASSIGNMENT_TTL, worker_id)
            return True
        except Exception as e:
            logger.error(f"Failed to set session assignment {session_id} -> {worker_id}: {e}")
            return False
    
    async def get_session_assignment(self, session_id: str) -> Optional[str]:
        """Get session assignment."""
        try:
            assignment_key = f"session:{session_id}:assignment"
            worker_id = await self.redis.get(assignment_key)
            return worker_id.decode() if worker_id else None
        except Exception as e:
            logger.error(f"Failed to get session assignment for {session_id}: {e}")
            return None
    
    async def cleanup_stale_workers(self) -> List[str]:
        """Remove workers that haven't sent heartbeat recently."""
        try:
            all_worker_keys = await self.redis.keys("worker:*")
            stale_workers = []
            
            for key in all_worker_keys:
                key_parts = key.split(":")
                if len(key_parts) == 2:  # worker:id format
                    worker_id = key_parts[1]
                    heartbeat_key = f"worker:{worker_id}:heartbeat"
                    heartbeat_exists = await self.redis.exists(heartbeat_key)
                    
                    if not heartbeat_exists:
                        # Worker hasn't sent heartbeat, mark as stale
                        stale_workers.append(worker_id)
                        await self.redis.delete(key)
                        # Also clean up their session queue
                        session_queue_key = f"worker:{worker_id}:sessions"
                        await self.redis.delete(session_queue_key)
                        logger.info(f"Cleaned up stale worker: {worker_id}")
            
            return stale_workers
        except Exception as e:
            logger.error(f"Failed to cleanup stale workers: {e}")
            return []


# Global Redis manager instance
redis_manager = RedisManager()