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
from .hitl import (
    HitlPendingInfo,
    HitlResolvedInfo,
    clear_pending_hitl,
    get_pending_hitl,
    get_resolved_hitl,
    get_session_pending_hitl,
    resolve_hitl,
    store_pending_hitl,
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
    "HitlPendingInfo",
    "HitlResolvedInfo",
    "store_pending_hitl",
    "get_pending_hitl",
    "resolve_hitl",
    "get_resolved_hitl",
    "clear_pending_hitl",
    "get_session_pending_hitl",
]
