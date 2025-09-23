"""
Simplified data models for single-use Daily.co room pool service.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ReadyRoomToken:
    """Single-use room with pre-generated token"""
    room_url: str
    token: str
    session_id: str  # For logging/debugging
    created_at: datetime
    expires_at: datetime

    def is_expired(self, buffer_minutes: int = 2) -> bool:
        """Check if token will expire soon"""
        buffer_seconds = buffer_minutes * 60
        return (self.expires_at.timestamp() - time.time()) < buffer_seconds

    def age_seconds(self) -> float:
        """Age of room+token in seconds"""
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()


@dataclass
class PoolConfig:
    """Simplified pool configuration"""
    target_pool_size: int = 15
    min_threshold: int = 5
    max_pool_size: int = 30  # Prevent unbounded growth
    token_expiry_buffer_minutes: int = 2
    maintenance_interval_seconds: int = 30
    batch_creation_limit: int = 5
    api_retry_attempts: int = 3
    api_retry_delay_seconds: int = 1
    enable_gradual_rollout: bool = False
    rollout_percentage: int = 0

    def validate(self) -> None:
        """Validate configuration values"""
        if self.min_threshold < 1:
            raise ValueError("min_threshold must be at least 1")
        if self.target_pool_size < self.min_threshold:
            raise ValueError("target_pool_size must be >= min_threshold")
        if self.max_pool_size < self.target_pool_size:
            raise ValueError("max_pool_size must be >= target_pool_size")
        if self.token_expiry_buffer_minutes < 1:
            raise ValueError("token_expiry_buffer_minutes must be at least 1")
        if not 0 <= self.rollout_percentage <= 100:
            raise ValueError("rollout_percentage must be between 0 and 100")


@dataclass
class PoolStats:
    """Simple pool statistics"""
    pool_size: int
    target_size: int
    rooms_created: int
    rooms_served: int
    fallback_used: int
    creation_errors: int
    expired_cleaned: int
    pool_hit_rate: float
    uptime_hours: float

    @property
    def health_status(self) -> str:
        """Determine pool health status"""
        if self.pool_size == 0:
            return "degraded"
        elif self.pool_size < 3:
            return "warning"
        else:
            return "healthy"