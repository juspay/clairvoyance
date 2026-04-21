"""
AlertConfig dataclass for the Pickup Rate Alert system.

Supports both global alerting and per-merchant scoping via merchant_id.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AlertConfig:
    """Configuration for a single pickup-rate alert scope.

    Fields:
        enabled:            Whether alerting is active for this scope.
        interval_seconds:   How often (in seconds) to check and potentially alert.
                            Also used as the Redis TTL for the dedup key.
        threshold_percent:  Alert fires when pickup rate drops below this value (0-100).
        alert_type:         Which rate(s) to evaluate.
                            "CALL_BASED"  - only call-based pickup rate.
                            "LEAD_BASED"  - only lead-based pickup rate.
                            "BOTH"        - alert if *either* rate is below threshold.
        scope:              Human-readable label used in Slack messages and Redis keys
                            (e.g. "global", "merchant:acme/store-1").
        lookback_hours:     Rolling window of call data to evaluate (default: 24 h).

    Optional scope fields:
        merchant_id:   Merchant identifier to scope DB queries (None = all merchants).
    """

    enabled: bool
    interval_seconds: int
    threshold_percent: float
    alert_type: str = "BOTH"
    scope: str = "global"
    lookback_hours: int = 24

    # Per-merchant scoping - wired through to calculator for DB query filtering
    merchant_id: Optional[str] = None

    # Computed Redis dedup key - derived from scope, not configurable directly
    _redis_key: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        safe_scope = self.scope.replace(":", "_").replace("/", "_")
        self._redis_key = f"pickup_rate:{safe_scope}:last_alert"

    @property
    def redis_dedup_key(self) -> str:
        """Redis key used to track last-alert timestamp for this scope."""
        return self._redis_key
