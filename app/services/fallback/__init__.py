"""Generic Redis-backed service fallback.

State machine:
  NORMAL  -- failure_count >= threshold --> FALLBACK_ACTIVE
  FALLBACK_ACTIVE  -- TTL expires after duration_secs --> NORMAL
  NORMAL  -- failures again --> FALLBACK_ACTIVE (repeat cycle)

Redis keys:
  fallback:config:{service}              - JSON config (permanent, managed externally)
  fallback:{service}:failure_count       - rolling failure counter with TTL=window_secs
  fallback:{service}:active              - flag with TTL=duration_secs (self-expires)
  fallback:{service}:notified            - NX dedup for one-time activation alert
  fallback:{service}:alerted:{count}     - NX dedup so only one pod fires per-failure alert
  buddy:{service}:health:{provider}      - "unhealthy" marker for monitoring (TTL=duration_secs)

Consumers check ``is_active()`` to decide whether to route to the fallback provider.
The active key self-expires after exactly duration_secs from activation time.
The background reset task fires the reset Slack alert when it detects expiry.
"""

from dataclasses import dataclass

from app.core.background_tasks import BackgroundTaskScheduler
from app.core.config.dynamic import BB_FALLBACK_RAW_CONFIG, BB_STT_SERVICE
from app.core.config.static import SLACK_TAG_USERS
from app.core.logger import logger
from app.services.redis.client import get_redis_service
from app.services.slack import slack_alert

# Slack tag used for all fallback alerts
_FALLBACK_TAG = "@breeze-sentinals"
_ALERT_TAG = f"{_FALLBACK_TAG},{SLACK_TAG_USERS}" if SLACK_TAG_USERS else _FALLBACK_TAG

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
class FallbackSettings:
    """Typed config for a single service's fallback behaviour.

    Sourced from the ``BB_FALLBACK`` DevCycle/Redis flag, keyed by service name.

    Attributes:
        enabled: Whether fallback is active for this service.
        fallback_provider: Provider to route to when primary fails.
        threshold: Failure count within window_secs to trip the circuit.
        duration_secs: How long the fallback stays active before auto-reset.
        window_secs: Rolling window for the failure counter TTL.
    """

    enabled: bool = False
    fallback_provider: str = ""
    threshold: int = 2
    duration_secs: int = 1800
    window_secs: int = 240


# Per-service sensible defaults for fallback_provider.
# Add "tts" and "llm" entries in their respective future PRs.
_FALLBACK_DEFAULTS: dict[str, FallbackSettings] = {
    "stt": FallbackSettings(fallback_provider="deepgram"),
    # "tts": FallbackSettings(fallback_provider="cartesia"),  # future PR
    # "llm": FallbackSettings(fallback_provider="openai"),    # future PR
}


async def BB_FALLBACK_CONFIG(service: str) -> FallbackSettings:
    """Return typed fallback config for the given service.

    Merges the raw Redis dict from ``BB_FALLBACK_RAW_CONFIG`` with the
    per-service defaults defined in ``_FALLBACK_DEFAULTS``.  Callers always
    receive a valid ``FallbackSettings`` — never ``None`` or a bare dict.
    """
    defaults = _FALLBACK_DEFAULTS.get(service, FallbackSettings())
    service_cfg = await BB_FALLBACK_RAW_CONFIG(service)
    if not service_cfg:
        return defaults
    return FallbackSettings(
        enabled=service_cfg.get("enabled", defaults.enabled),
        fallback_provider=service_cfg.get(
            "fallback_provider", defaults.fallback_provider
        ),
        threshold=service_cfg.get("threshold", defaults.threshold),
        duration_secs=service_cfg.get("duration_secs", defaults.duration_secs),
        window_secs=service_cfg.get("window_secs", defaults.window_secs),
    )


@dataclass
class ServiceFallbackConfig:
    """Configuration for a Redis-backed service fallback.

    Attributes:
        service_name: Identifier used in Redis keys and logs (e.g., "stt")
        failure_threshold: Number of failures within window to activate fallback
        failure_window_secs: Sliding window for failure counter TTL
        fallback_duration_secs: How long fallback stays active
        primary_provider_name: Human-readable primary provider name (for alerts/logs)
        fallback_provider_name: Human-readable fallback provider name (for alerts/logs)
    """

    service_name: str
    failure_threshold: int = 2
    failure_window_secs: int = 240
    fallback_duration_secs: int = 1800
    primary_provider_name: str = "primary"
    fallback_provider_name: str = "fallback"


class ServiceFallback:
    """Generic Redis-backed service fallback.

    On activation, sets a Redis flag. Consumers check ``is_active()`` to route
    to the fallback provider. A background task clears the flag on schedule.

    Inline Slack alerts are sent at each key lifecycle event:
      - Each failure: ⚠️ STT Failure on {Provider} ({count}/{threshold})
      - Activation:   🔴 {Service} Fallback Activated — {Provider}
      - Reset:        ✅ {Service} Fallback Reset — Back to {Provider}
    """

    def __init__(self, config: ServiceFallbackConfig):
        self.config = config
        self._key_failure_count = f"fallback:{config.service_name}:failure_count"
        self._key_active = f"fallback:{config.service_name}:active"
        self._key_notified = f"fallback:{config.service_name}:notified"
        self._key_alerted_prefix = f"fallback:{config.service_name}:alerted"
        self._key_health_prefix = f"buddy:{config.service_name}:health"
        # Persistent sentinel set by the background poller while fallback is
        # active.  When the poller next runs and finds is_active() False but
        # this sentinel present, it knows the TTL just expired and fires the
        # reset alert.  No TTL — cleared explicitly on reset.
        self._key_seen_active = f"fallback:{config.service_name}:seen_active"

    # ------------------------------------------------------------------
    # Inline alert helpers
    # ------------------------------------------------------------------

    async def _send_failure_alert(self, count: int, error_msg: str) -> None:
        provider = self.config.primary_provider_name.capitalize()
        threshold = self.config.failure_threshold
        try:
            await slack_alert.send(
                title=f"⚠️ STT Failure on {provider} ({count}/{threshold})",
                fields=[{"name": "Fail Count", "value": f"{count}/{threshold}"}],
                sections=(
                    [{"title": "Error", "text": f"```{error_msg}```"}]
                    if error_msg
                    else []
                ),
                fallback_text=f"STT failure on {provider} ({count}/{threshold})",
                tag_users=_ALERT_TAG,
            )
        except Exception as e:
            logger.warning(
                f"Service fallback ({self.config.service_name}) "
                f"failure alert failed: {e}"
            )

    async def _send_activation_alert(self, cooldown_min: int) -> None:
        primary = self.config.primary_provider_name.capitalize()
        fallback = self.config.fallback_provider_name.capitalize()
        service = self.config.service_name.upper()
        try:
            await slack_alert.send(
                title=f"🔴 {service} Fallback Activated — {primary}",
                fields=[{"name": "Duration", "value": f"{cooldown_min} minutes"}],
                sections=[
                    {
                        "title": "What Happened",
                        "text": (
                            f"{primary} {service} hit {self.config.failure_threshold} failures. "
                            f"All new calls will use {fallback} for {cooldown_min} minutes. "
                            f"{primary} will be retried automatically after the duration expires."
                        ),
                    }
                ],
                fallback_text=f"{service} fallback activated — {fallback} for {cooldown_min} min",
                tag_users=_ALERT_TAG,
            )
        except Exception as e:
            logger.warning(
                f"Service fallback ({self.config.service_name}) "
                f"activation alert failed: {e}"
            )

    async def _send_reset_alert(self) -> None:
        primary = self.config.primary_provider_name.capitalize()
        service = self.config.service_name.upper()
        try:
            await slack_alert.send(
                title=f"✅ {service} Fallback Reset — Back to {primary}",
                sections=[
                    {
                        "title": "What Happened",
                        "text": (
                            f"{service} fallback duration expired. "
                            f"Calls are back on primary {primary} provider. "
                            "Normal operation resumed."
                        ),
                    }
                ],
                fallback_text=f"{service} fallback reset — back to {primary}",
                tag_users=_ALERT_TAG,
            )
        except Exception as e:
            logger.warning(
                f"Service fallback ({self.config.service_name}) "
                f"reset alert failed: {e}"
            )

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    async def _mark_provider_unhealthy(self, redis, provider: str) -> None:
        """Set a monitoring health key for the given provider."""
        try:
            health_key = f"{self._key_health_prefix}:{provider}"
            await redis.set(
                health_key,
                "unhealthy",
                ex=self.config.fallback_duration_secs,
            )
        except Exception as e:
            logger.warning(
                f"Service fallback ({self.config.service_name}) "
                f"health mark failed: {e}"
            )

    async def _clear_provider_health(self, redis, provider: str) -> None:
        """Clear the monitoring health key for the given provider."""
        try:
            health_key = f"{self._key_health_prefix}:{provider}"
            await redis.delete(health_key)
        except Exception as e:
            logger.warning(
                f"Service fallback ({self.config.service_name}) "
                f"health clear failed: {e}"
            )

    # ------------------------------------------------------------------
    # Core state machine
    # ------------------------------------------------------------------

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
            count = await redis.run_script(
                _LUA_INCR_WITH_EXPIRE,
                keys=[self._key_failure_count],
                args=[self.config.failure_window_secs],
            )
            if count is None:
                count = await redis.incr(self._key_failure_count)

            logger.info(
                f"Service fallback ({self.config.service_name}): "
                f"failure {count}/{self.config.failure_threshold}"
            )

            # Per-failure alert — deduplicated with NX so only the first pod fires.
            alert_dedup_key = f"{self._key_alerted_prefix}:{count}"
            is_first = await redis.set(
                alert_dedup_key,
                "1",
                nx=True,
                ex=self.config.failure_window_secs,
            )
            if is_first:
                await self._send_failure_alert(
                    count=count,
                    error_msg=error_msg[:500] if error_msg else "",
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
        the configured cooldown. NX ensures only one pod activates.
        """
        newly_set = await redis.set(
            self._key_active, "1", nx=True, ex=self.config.fallback_duration_secs
        )
        if not newly_set:
            return

        # Clear failure counter
        await redis.delete(self._key_failure_count)

        # Mark primary provider unhealthy for monitoring
        await self._mark_provider_unhealthy(
            redis, self.config.primary_provider_name.lower()
        )

        cooldown_min = self.config.fallback_duration_secs // 60
        logger.warning(
            f"Service fallback ({self.config.service_name}) ACTIVATED "
            f"(duration={self.config.fallback_duration_secs}s, "
            f"fallback={self.config.fallback_provider_name})"
        )

        # Activation alert — deduplicated with NX so only one pod fires.
        notified = await redis.set(
            self._key_notified,
            "1",
            nx=True,
            ex=self.config.fallback_duration_secs,
        )
        if notified:
            await self._send_activation_alert(cooldown_min=cooldown_min)

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
        """Reset to primary: clear fallback flag and send reset alert."""
        try:
            redis = await get_redis_service()

            await redis.delete(self._key_active)
            await redis.delete(self._key_failure_count)
            await redis.delete(self._key_notified)

            # Clear health mark for primary provider
            await self._clear_provider_health(
                redis, self.config.primary_provider_name.lower()
            )

            logger.info(
                f"Service fallback ({self.config.service_name}) reset to primary"
            )

            await self._send_reset_alert()
        except Exception as e:
            logger.error(
                f"Service fallback ({self.config.service_name}) "
                f"reset_to_primary failed: {e}"
            )

    async def notify_on_expiry(self) -> None:
        """Poll-friendly check: send reset alert exactly when TTL expires.

        Called by the background task on a short interval (e.g. 60 s).

        - If fallback is still active:   mark sentinel and return.
        - If fallback just expired (sentinel set, key gone): send reset alert
          and clear sentinel so the alert fires only once.
        - If neither: nothing to do (sentinel not set means we never saw it
          active in this server lifetime).
        """
        try:
            redis = await get_redis_service()
            active = bool(await redis.exists(self._key_active))

            if active:
                # Still within the fallback window — record that we've seen it.
                await redis.set(self._key_seen_active, "1")
                return

            # Not active — check if it *was* active (sentinel present).
            seen = bool(await redis.exists(self._key_seen_active))
            if not seen:
                return  # Never activated during this server lifetime.

            # TTL just expired — clear sentinel and fire the reset alert.
            await redis.delete(self._key_seen_active)
            await self._clear_provider_health(
                redis, self.config.primary_provider_name.lower()
            )
            logger.info(
                f"Service fallback ({self.config.service_name}) TTL expired — "
                "sending reset alert"
            )
            await self._send_reset_alert()
        except Exception as e:
            logger.error(
                f"Service fallback ({self.config.service_name}) "
                f"notify_on_expiry failed: {e}"
            )


# ---------------------------------------------------------------------------
#  STT Fallback Background Task
# ---------------------------------------------------------------------------


async def check_and_reset_stt_fallback() -> None:
    """Poll STT fallback state and fire the reset alert when TTL expires.

    Runs every 60 s.  Delegates to ``ServiceFallback.notify_on_expiry()``
    which sets a sentinel while the fallback is active and fires the ✅ reset
    alert the first time it observes the active key has expired.  The Redis
    TTL on the active key remains the sole source of truth for when routing
    reverts to primary — this task only handles the Slack notification.
    """
    try:
        cfg = await BB_FALLBACK_CONFIG("stt")
        primary_provider = await BB_STT_SERVICE()
        fallback = ServiceFallback(
            ServiceFallbackConfig(
                service_name="stt",
                failure_threshold=cfg.threshold,
                failure_window_secs=cfg.window_secs,
                fallback_duration_secs=cfg.duration_secs,
                primary_provider_name=primary_provider,
                fallback_provider_name=cfg.fallback_provider,
            )
        )
        await fallback.notify_on_expiry()
    except Exception as e:
        logger.error(f"STT fallback reset task failed: {e}")


async def initialize_fallback_tasks(scheduler: BackgroundTaskScheduler) -> None:
    """Register STT fallback reset task if fallback is enabled."""
    cfg = await BB_FALLBACK_CONFIG("stt")
    if not cfg.enabled:
        logger.info("STT fallback disabled — skipping fallback task registration")
        return

    scheduler.register_task(
        name="stt_fallback_reset",
        func=check_and_reset_stt_fallback,
        interval_seconds=60,
    )
    logger.info("Registered STT fallback reset task (interval=60s)")
