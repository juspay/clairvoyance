"""
Redis Client Configuration

Provides Redis connection for pipeline caching and conversation history.
"""

import json
from typing import Any, Optional

import redis

from app.core.config import REDIS_URL
from app.core.logger import logger


class RedisClient:
    """Redis client wrapper with JSON serialization."""

    def __init__(self):
        self._client = None

    def get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            try:
                self._client = redis.from_url(
                    REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )
                # Test connection
                self._client.ping()
                logger.info("Redis connection established")
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}, using memory fallback")
                # Fallback to fake Redis for development
                self._client = FakeRedis()
        return self._client

    async def get(self, key: str) -> Optional[Any]:
        """Get and deserialize value from Redis."""
        try:
            client = self.get_client()
            value = client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            logger.error(f"Redis get error for key {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        """Serialize and set value in Redis."""
        try:
            client = self.get_client()
            serialized = json.dumps(value, default=str)
            result = client.set(key, serialized, ex=ex)
            return bool(result)
        except Exception as e:
            logger.error(f"Redis set error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from Redis."""
        try:
            client = self.get_client()
            result = client.delete(key)
            return bool(result)
        except Exception as e:
            logger.error(f"Redis delete error for key {key}: {e}")
            return False

    async def exists(self, key: str) -> bool:
        """Check if key exists in Redis."""
        try:
            client = self.get_client()
            return bool(client.exists(key))
        except Exception as e:
            logger.error(f"Redis exists error for key {key}: {e}")
            return False


class FakeRedis:
    """In-memory fallback for Redis when connection fails."""

    def __init__(self):
        self._data = {}

    def ping(self):
        return True

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None):
        self._data[key] = value
        return True

    def delete(self, key: str):
        return self._data.pop(key, None) is not None

    def exists(self, key: str):
        return key in self._data


# Global Redis client instance
_redis_client = RedisClient()


def get_redis_client() -> RedisClient:
    """Get the global Redis client instance."""
    return _redis_client
