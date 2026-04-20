"""
Service Health Monitor

Tracks upstream service failures via Redis sliding-window counters.
When error counts exceed configured thresholds, calls are paused globally
via a Redis overlay flag (no database mutation). A background task runs
every 60 seconds to evaluate all rules and auto-resume after a recovery
window.

All error log messages across the app flow through a Loguru sink registered
here. The sink matches each WARNING+ message against ``log_patterns`` defined
in ``rules.json`` and increments the corresponding Redis counter. Nothing in
providers or agents needs to be modified — they just call
``logger.error(...)`` as usual.

To add monitoring for a new service, add an entry to ``rules.json``.
No Python changes required.

Redis key layout:
  {service_health}:errors:{rule}:{minute_bucket}  — integer counter, TTL 600s
  {service_health}:global_paused                  — "true" when paused
  {service_health}:pause_reason                   — human-readable string
  {service_health}:paused_by                      — "auto" or user_id
  {service_health}:paused_at                      — ISO-8601 UTC
  {service_health}:source_rule                    — rule name that triggered pause
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config.dynamic import (
    ENABLE_SERVICE_HEALTH_MONITORING,
    SERVICE_HEALTH_AUTO_RESUME_MINUTES,
)
from app.core.logger import logger
from app.services.redis import get_redis_service
from app.services.slack.alert import Alert

# Slack alert singleton
_slack_alert = Alert()

# Hash tag ensures all keys land on same Redis Cluster slot for multi-key commands
_NS = "{service_health}"


def _load_rules() -> dict:
    """
    Load rule definitions from rules.json (co-located with this file).

    Each entry has the shape::

        "<rule_name>": {
            "threshold_count": int,
            "window_minutes":  int,
            "log_patterns":    [str, ...]   # lowercase phrases to match in logs
        }

    Returns an empty dict and logs a warning if the file is missing or malformed.
    """
    rules_path = Path(__file__).parent / "rules.json"
    try:
        with rules_path.open() as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"[ServiceHealth] Could not load rules.json: {exc}")
        return {}


# Module-level constants loaded once at import time
_RULES: dict = _load_rules()
_BUCKET_TTL = max(
    max((cfg["window_minutes"] for cfg in _RULES.values()), default=5) * 120,
    600,
)
_LOG_PATTERN_MAP: dict[str, list[str]] = {
    rule: cfg["log_patterns"] for rule, cfg in _RULES.items() if cfg.get("log_patterns")
}


def _make_log_health_sink():
    pattern_map = _LOG_PATTERN_MAP

    def sink(message):
        record = message.record
        if record["level"].no < 40:  # ERROR only
            return
        # Prevent infinite loops when service_health logs errors
        if record["name"].startswith("app.services.service_health"):
            return
        text = record["message"].lower()
        try:
            loop = asyncio.get_running_loop()
            for rule, phrases in pattern_map.items():
                if any(phrase in text for phrase in phrases):
                    loop.create_task(service_health_monitor.record_error(rule))
        except Exception:
            pass  # Never let health monitoring affect normal logging

    return sink


def install_log_sink() -> None:
    """Register the service-health sink with Loguru."""
    logger.add(
        _make_log_health_sink(),
        level="ERROR",
        filter=lambda r: True,
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )
    logger.info("[ServiceHealth] Log-pattern health sink installed")


class ServiceHealthMonitor:
    """Monitors service health and auto-pauses/resumes outbound calls via Redis overlay."""

    async def record_error(self, rule: str) -> None:
        """Increment per-minute error bucket for a rule. Non-blocking."""
        if not await ENABLE_SERVICE_HEALTH_MONITORING():
            return

        try:
            redis = await get_redis_service()
            bucket = int(time.time()) // 60
            key = f"{_NS}:errors:{rule}:{bucket}"
            count = await redis.incr(key)
            # Only set TTL on the first write to avoid hammering Redis
            if count == 1:
                await redis.expire(key, _BUCKET_TTL)  # TTL = 2x max window (min 10 min)
        except Exception as e:
            logger.warning(
                f"[ServiceHealth] Failed to record error for rule '{rule}': {e}"
            )

    async def get_error_count(self, rule: str, window_minutes: int) -> int:
        """Sum errors for a rule across the sliding window."""
        try:
            redis = await get_redis_service()
            client = await redis.get_client()
            now_bucket = int(time.time()) // 60
            keys = [
                f"{_NS}:errors:{rule}:{now_bucket - i}" for i in range(window_minutes)
            ]
            # MGET returns a list of values (bytes or None)
            raw_values = await client.mget(keys)
            total = sum(int(v) for v in raw_values if v is not None)
            return total
        except Exception as e:
            logger.warning(
                f"[ServiceHealth] Failed to get error count for rule '{rule}': {e}"
            )
            return 0

    async def is_globally_paused(self) -> bool:
        """Check if global pause overlay is active."""
        try:
            redis = await get_redis_service()
            value = await redis.get(f"{_NS}:global_paused")
            return value in ("true", b"true")
        except Exception as e:
            logger.warning(f"[ServiceHealth] Failed to check global pause state: {e}")
            return False

    async def pause_calls(
        self,
        reason: str,
        paused_by: str = "auto",
        source_rule: Optional[str] = None,
    ) -> None:
        """
        Activate the global call-pause overlay.

        Writes five Redis keys that callers can inspect. The database
        ``enable_calling`` column is intentionally never touched.
        """
        try:
            redis = await get_redis_service()
            client = await redis.get_client()
            paused_at = datetime.now(timezone.utc).isoformat()
            mapping = {
                f"{_NS}:global_paused": "true",
                f"{_NS}:pause_reason": reason,
                f"{_NS}:paused_by": paused_by,
                f"{_NS}:paused_at": paused_at,
                f"{_NS}:source_rule": source_rule or "",
            }
            await client.mset(mapping)
            logger.warning(
                f"[ServiceHealth] Calls PAUSED — rule={source_rule!r}, reason={reason!r}, by={paused_by!r}"
            )
        except Exception as e:
            logger.error(f"[ServiceHealth] Failed to set global pause: {e}")

    async def resume_calls(self, resumed_by: str = "auto") -> None:
        """
        Clear the global call-pause overlay, allowing calls to proceed.

        Only the five overlay keys are deleted. Per-merchant DB settings
        are untouched.
        """
        try:
            redis = await get_redis_service()
            client = await redis.get_client()
            keys = [
                f"{_NS}:global_paused",
                f"{_NS}:pause_reason",
                f"{_NS}:paused_by",
                f"{_NS}:paused_at",
                f"{_NS}:source_rule",
                f"{_NS}:clear_since",
            ]
            await client.delete(*keys)
            logger.info(f"[ServiceHealth] Calls RESUMED by={resumed_by!r}")
        except Exception as e:
            logger.error(f"[ServiceHealth] Failed to clear global pause: {e}")

    async def get_status(self) -> dict:
        """
        Return a snapshot of the current service health state.

        Includes the pause overlay fields and per-rule error counts
        (measured over each rule's configured window).
        """
        try:
            redis = await get_redis_service()
            client = await redis.get_client()
            keys = [
                f"{_NS}:global_paused",
                f"{_NS}:pause_reason",
                f"{_NS}:paused_by",
                f"{_NS}:paused_at",
                f"{_NS}:source_rule",
                f"{_NS}:clear_since",
            ]
            values = await client.mget(keys)
            is_paused = values[0] == b"true" or values[0] == "true"

            rule_counts = {}
            for rule, cfg in _RULES.items():
                rule_counts[rule] = {
                    "error_count": await self.get_error_count(
                        rule, cfg["window_minutes"]
                    ),
                    "threshold": cfg["threshold_count"],
                    "window_minutes": cfg["window_minutes"],
                }

            def _decode(v):
                if v is None:
                    return None
                return v.decode() if isinstance(v, bytes) else v

            return {
                "is_paused": is_paused,
                "pause_reason": _decode(values[1]),
                "paused_by": _decode(values[2]),
                "paused_at": _decode(values[3]),
                "source_rule": _decode(values[4]),
                "rules": rule_counts,
            }
        except Exception as e:
            logger.error(f"[ServiceHealth] Failed to fetch status: {e}")
            return {"is_paused": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # Background health check (runs every 60 s via BackgroundTaskScheduler)
    # ------------------------------------------------------------------ #

    async def run_auto_health_check(self) -> None:
        """
        Evaluate all rules and toggle the global pause overlay accordingly.

        Logic:
        - If currently NOT paused and any rule exceeds its threshold → pause + Slack alert
        - If currently paused by "auto" and all rules are clear AND the pause
          has exceeded SERVICE_HEALTH_AUTO_RESUME_MINUTES → resume + Slack alert
        - If currently paused by a human ("paused_by" != "auto") → never auto-resume
        """
        if not await ENABLE_SERVICE_HEALTH_MONITORING():
            return

        currently_paused = await self.is_globally_paused()

        if not currently_paused:
            for rule, cfg in _RULES.items():
                threshold = cfg["threshold_count"]
                window = cfg["window_minutes"]
                count = await self.get_error_count(rule, window)
                if count >= threshold:
                    reason = (
                        f"Auto-paused: rule '{rule}' hit {count}/{threshold} errors "
                        f"in the last {window} minute(s)"
                    )
                    await self.pause_calls(
                        reason=reason, paused_by="auto", source_rule=rule
                    )
                    await self._send_pause_slack_alert(rule, count, threshold, window)
                    return  # One pause per cycle is enough
        else:
            # Only auto-resume if the pause was system-triggered
            redis = await get_redis_service()
            paused_by = await redis.get(f"{_NS}:paused_by")
            if paused_by not in ("auto", b"auto"):
                # Human-set pause — never auto-resume
                return

            # Check whether all rules are back below threshold
            all_clear = True
            for rule, cfg in _RULES.items():
                threshold = cfg["threshold_count"]
                window = cfg["window_minutes"]
                count = await self.get_error_count(rule, window)
                if count >= threshold:
                    all_clear = False
                    break

            if not all_clear:
                # Errors still above threshold — clear the "clean since" marker
                await redis.delete(f"{_NS}:clear_since")
                return

            # All rules are clear — track when they FIRST became clear
            auto_resume_minutes = await SERVICE_HEALTH_AUTO_RESUME_MINUTES()
            clear_since_raw = await redis.get(f"{_NS}:clear_since")

            if not clear_since_raw:
                # First moment all rules are clear — record the timestamp
                now_iso = datetime.now(timezone.utc).isoformat()
                await redis.set(f"{_NS}:clear_since", now_iso)
                await redis.expire(f"{_NS}:clear_since", 3600)  # 1-hour TTL safety net
                return  # Wait for the next check cycle

            # Parse the timestamp and check if enough clean time has elapsed
            clear_since_str = (
                clear_since_raw.decode()
                if isinstance(clear_since_raw, bytes)
                else clear_since_raw
            )
            try:
                clear_since = datetime.fromisoformat(clear_since_str)
                clear_minutes = (
                    datetime.now(timezone.utc) - clear_since
                ).total_seconds() / 60
                if clear_minutes < auto_resume_minutes:
                    return  # Not enough clean time yet
            except Exception:
                pass  # Corrupt timestamp — proceed with resume

            source_rule = await redis.get(f"{_NS}:source_rule")
            if isinstance(source_rule, bytes):
                source_rule = source_rule.decode()

            await self.resume_calls(resumed_by="auto")
            await self._send_resume_slack_alert(source_rule)

    # ------------------------------------------------------------------ #
    # Slack helpers
    # ------------------------------------------------------------------ #

    async def _send_pause_slack_alert(
        self, rule: str, count: int, threshold: int, window: int
    ) -> None:
        try:
            await _slack_alert.send(
                title="🚨 Breeze Buddy — Calls Auto-Paused",
                fields=[
                    {"name": "Rule", "value": rule},
                    {
                        "name": "Errors",
                        "value": f"{count} in {window} min (threshold: {threshold})",
                    },
                    {
                        "name": "Action",
                        "value": "Outbound calls have been paused automatically",
                    },
                    {
                        "name": "Resume",
                        "value": (
                            f"Will auto-resume after {await SERVICE_HEALTH_AUTO_RESUME_MINUTES()} min "
                            "once errors clear, or resume manually via Loom dashboard"
                        ),
                    },
                ],
            )
        except Exception as e:
            logger.warning(f"[ServiceHealth] Failed to send pause Slack alert: {e}")

    async def _send_resume_slack_alert(self, source_rule: Optional[str]) -> None:
        try:
            await _slack_alert.send(
                title="✅ Breeze Buddy — Calls Auto-Resumed",
                fields=[
                    {"name": "Previous Rule", "value": source_rule or "unknown"},
                    {
                        "name": "Status",
                        "value": "Errors are below threshold — calls resumed",
                    },
                ],
            )
        except Exception as e:
            logger.warning(f"[ServiceHealth] Failed to send resume Slack alert: {e}")


# Module-level singleton — imported by the Loguru sink and API endpoints
service_health_monitor = ServiceHealthMonitor()
