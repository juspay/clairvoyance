"""
Simplified Redis Services Module

Provides basic Redis client functionality for the Clairvoyance platform.
"""

from .client import (
    RedisFactory,
    RedisService,
    close_redis_connections,
    get_redis_service,
    is_redis_configured,
)
from .locks import LockAcquireError, RedisLock

__all__ = [
    "RedisFactory",
    "RedisService",
    "get_redis_service",
    "close_redis_connections",
    "is_redis_configured",
    "RedisLock",
    "LockAcquireError",
]
