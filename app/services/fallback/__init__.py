"""Generic Redis-backed service fallback.

State machine:
  NORMAL  -- failure_count >= threshold --> FALLBACK_ACTIVE
  FALLBACK_ACTIVE  -- TTL expires after duration_secs --> NORMAL
  NORMAL  -- failures again --> FALLBACK_ACTIVE (repeat cycle)

Redis keys:
  fallback:{service_name}:failure_count      - rolling failure counter with TTL window
  fallback:{service_name}:active             - flag with TTL=fallback_duration_secs (self-expires)
  fallback:{service_name}:notified           - NX dedup for one-time activation alert
  fallback:{service_name}:alerted:{count}    - NX dedup so only one pod fires per-failure alert

Consumers check ``is_active()`` to decide whether to route to the fallback provider.
The active key self-expires after exactly fallback_duration_secs from activation time.
The background reset task is a secondary mechanism that fires the on_reset_alert callback.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app.core.background_tasks import BackgroundTaskScheduler
from app.core.config.dynamic import (
    BB_STT_FALLBACK_DURATION_SECS,
    BB_STT_FALLBACK_PROVIDER,
    BB_STT_FALLBACK_THRESHOLD,
    BB_STT_FALLBACK_WINDOW_SECS,
    ENABLE_BB_STT_FALLBACK,
)
from app.core.logger import logger
from app.services.redis.client import get_redis_service

# Type alias for alert callback.
AlertCallback = Callable[..., Awaitable[None]]

# Lua script: atomically INCR the failure counter and set TTL on first write.
# Returns the new count. Using Lua ensures INCR + EXPIRE are one indivisible
# operation — eliminates the race where another pod deletes the key between
# the two calls and the EXPIRE lands on a brand-new counter with no TTL.
_LUA_INCR_WITH_EXPIRE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@dataclass
class ServiceFallbackConfig:
    """Configuration for a Redis-backed service fallback.

    Attributes:
        service_name: Identifier used in Redis keys and logs (e.g., "stt")
        failure_threshold: Number of failures within window to activate fallback
        failure_window_secs: Sliding window for failure counter TTL
        fallback_duration_secs: How long fallback stays active (background task interval)
        fallback_provider_name: Human-readable name for the fallback provider (for alerts/logs)
        key_prefix: Prefix for Redis keys (default: "fallback"). Use "circuit" or "health" for non-fallback use cases.
        on_failure_alert: Callback fired on each failure
        on_trip_alert: Callback fired when fallback activates
        on_reset_alert: Callback fired when background task resets to primary
    """

    service_name: str
    failure_threshold: int = 2
    failure_window_secs: int = 240
    fallback_duration_secs: int = 1800
    fallback_provider_name: str = "fallback"
    key_prefix: str = "fallback"
    on_failure_alert: Optional[AlertCallback] = None
    on_trip_alert: Optional[AlertCallback] = None
    on_reset_alert: Optional[AlertCallback] = None


class ServiceFallback:
    """Generic Redis-backed service fallback.

    On activation, sets a Redis flag. Consumers check ``is_active()`` to route
    to the fallback provider. A background task clears the flag on schedule.
    """

    def __init__(self, config: ServiceFallbackConfig):
        self.config = config
        prefix = config.key_prefix
        self._key_failure_count = f"{prefix}:{config.service_name}:failure_count"
        self._key_active = f"{prefix}:{config.service_name}:active"
        self._key_notified = f"{prefix}:{config.service_name}:notified"
        self._key_alerted_prefix = f"{prefix}:{config.service_name}:alerted"

    async def record_failure(
        self,
        error_msg: str = "",
        call_sid: str = "",
        context: str = "unknown",
    ) -> bool:
        """Increment failure count. Activate fallback if threshold reached.

        Returns:
            True if this failure caused fallback activation, False otherwise.
        """
        try:
            redis = await get_redis_service()

            # Atomically increment and set TTL on first write via Lua script.
            # Eliminates the INCR+EXPIRE race where the key could be deleted
            # between the two calls, leaving the new counter with no TTL.
            count = await redis.run_script(
                _LUA_INCR_WITH_EXPIRE,
                keys=[self._key_failure_count],
                args=[self.config.failure_window_secs],
            )
            if count is None:
                # Lua script failed (logged inside run_script); fall back to
                # non-atomic path so failures are never silently swallowed.
                count = await redis.incr(self._key_failure_count)

            logger.info(
                f"Service fallback ({self.config.service_name}): "
                f"failure {count}/{self.config.failure_threshold}"
            )

            # Per-failure alert — deduplicated with NX so only the first pod
            # to reach count=N fires the Slack alert. Without this, every pod
            # that records the same failure would send its own alert.
            if self.config.on_failure_alert:
                alert_dedup_key = f"{self._key_alerted_prefix}:{count}"
                is_first = await redis.set(
                    alert_dedup_key,
                    "1",
                    nx=True,
                    ex=self.config.failure_window_secs,
                )
                if is_first:
                    try:
                        await self.config.on_failure_alert(
                            count=count,
                            threshold=self.config.failure_threshold,
                            error_msg=error_msg[:500] if error_msg else "",
                            call_sid=call_sid,
                            context=context,
                            service_name=self.config.service_name,
                        )
                    except Exception as alert_err:
                        logger.warning(
                            f"Service fallback ({self.config.service_name}) "
                            f"failure alert failed: {alert_err}"
                        )

            if count >= self.config.failure_threshold:
                await self._activate(redis)
                return True
            return False
        except Exception as e:
            logger.error(
                f"Service fallback ({self.config.service_name}) "
                f"record_failure failed: {e}"
            )
            return False

    async def _activate(self, redis) -> None:
        """Transition to FALLBACK_ACTIVE: set Redis flag.

        TTL = fallback_duration_secs ensures the key self-expires after exactly
        the configured cooldown, measured from activation time (not server start).
        This also prevents permanent stuck-fallback if the reset task is never
        registered (e.g. flag was off at startup and flipped later).
        """
        # NX ensures only one pod activates — others skip.
        # EX ensures the key self-expires after exactly fallback_duration_secs,
        # regardless of whether the background reset task runs.
        newly_set = await redis.set(
            self._key_active, "1", nx=True, ex=self.config.fallback_duration_secs
        )
        if not newly_set:
            return

        # Clear failure counter
        await redis.delete(self._key_failure_count)

        cooldown_min = self.config.fallback_duration_secs // 60
        logger.warning(
            f"Service fallback ({self.config.service_name}) ACTIVATED "
            f"(duration={self.config.fallback_duration_secs}s, "
            f"fallback={self.config.fallback_provider_name})"
        )

        # Trip alert
        if self.config.on_trip_alert:
            try:
                await self.config.on_trip_alert(
                    service_name=self.config.service_name,
                    fallback_name=self.config.fallback_provider_name,
                    threshold=self.config.failure_threshold,
                    cooldown_min=cooldown_min,
                )
            except Exception as alert_err:
                logger.warning(
                    f"Service fallback ({self.config.service_name}) "
                    f"trip alert failed: {alert_err}"
                )

    async def is_active(self) -> bool:
        """Check if fallback is currently active."""
        try:
            redis = await get_redis_service()
            return bool(await redis.exists(self._key_active))
        except Exception as e:
            logger.error(
                f"Service fallback ({self.config.service_name}) "
                f"is_active check failed: {e}"
            )
            return False

    async def reset_to_primary(self) -> None:
        """Reset to primary: clear fallback flag."""
        try:
            redis = await get_redis_service()

            # Clear fallback flag
            await redis.delete(self._key_active)
            # Clear failure counter
            await redis.delete(self._key_failure_count)
            # Clear notification dedup key
            await redis.delete(self._key_notified)

            logger.info(
                f"Service fallback ({self.config.service_name}) " f"reset to primary"
            )

            # Reset alert
            if self.config.on_reset_alert:
                try:
                    await self.config.on_reset_alert(
                        service_name=self.config.service_name,
                    )
                except Exception as alert_err:
                    logger.warning(
                        f"Service fallback ({self.config.service_name}) "
                        f"reset alert failed: {alert_err}"
                    )
        except Exception as e:
            logger.error(
                f"Service fallback ({self.config.service_name}) "
                f"reset_to_primary failed: {e}"
            )


# ---------------------------------------------------------------------------
#  STT Fallback Background Task
# ---------------------------------------------------------------------------


async def check_and_reset_stt_fallback() -> None:
    """Check if STT fallback is active and reset to primary if so."""
    try:
        fallback_provider = await BB_STT_FALLBACK_PROVIDER()
        fallback = ServiceFallback(
            ServiceFallbackConfig(
                service_name="stt",
                failure_threshold=await BB_STT_FALLBACK_THRESHOLD(),
                failure_window_secs=await BB_STT_FALLBACK_WINDOW_SECS(),
                fallback_duration_secs=await BB_STT_FALLBACK_DURATION_SECS(),
                fallback_provider_name=fallback_provider,
            )
        )
        if not await fallback.is_active():
            return

        logger.info("STT fallback active — resetting to primary provider")
        await fallback.reset_to_primary()
    except Exception as e:
        logger.error(f"STT fallback reset task failed: {e}")


async def initialize_fallback_tasks(scheduler: BackgroundTaskScheduler) -> None:
    """Register STT fallback reset task if fallback is enabled."""
    fallback_enabled = await ENABLE_BB_STT_FALLBACK()
    if not fallback_enabled:
        logger.info("STT fallback disabled — skipping fallback task registration")
        return

    duration_secs = await BB_STT_FALLBACK_DURATION_SECS()
    scheduler.register_task(
        name="stt_fallback_reset",
        func=check_and_reset_stt_fallback,
        interval_seconds=duration_secs,
    )
    logger.info(f"Registered STT fallback reset task (interval={duration_secs}s)")
