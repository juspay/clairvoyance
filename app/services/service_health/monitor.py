"""Service Health Monitor using ServiceFallback (circuit breaker).

Reuses ServiceFallback from app.services.fallback.__init__
Error capture: on_pipeline_error (same as STT fallback)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.background_tasks import BackgroundTaskScheduler
from app.core.config.dynamic import (
    ENABLE_SERVICE_HEALTH_MONITORING,
    SERVICE_HEALTH_AUTO_RESUME_MINUTES,
)
from app.core.logger import logger
from app.services.fallback import ServiceFallback, ServiceFallbackConfig
from app.services.redis.client import get_redis_service
from app.services.slack.alert import Alert

_slack_alert = Alert()

# Processor name → rule name mapping
# NOTE: STT errors (soniox, deepgram, etc.) are handled by STT fallback
# This covers TTS, LLM, and telephony transports
PROCESSOR_RULE_MAP = {
    # TTS providers
    "elevenlabs": "elevenlabs",
    "elevenlabsttsservice": "elevenlabs",
    "cartesia": "cartesia",
    "cartesiattsservice": "cartesia",
    # LLM providers
    "azure": "llm",
    "openaillm": "llm",
    "googlellm": "llm",
    # Telephony transports (Twilio, Exotel, Plivo)
    "twilio": "twilio",
    "twiliotransport": "twilio",
    "twilioinputtransport": "twilio",
    "twiliooutputtransport": "twilio",
    "exotel": "exotel",
    "exoteltransport": "exotel",
    "exotelinputtransport": "exotel",
    "exoteloutputtransport": "exotel",
    "plivo": "plivo",
    "plivotransport": "plivo",
    "plivoinputtransport": "plivo",
    "plivooutputtransport": "plivo",
}


def _load_rules() -> dict:
    """Load rule definitions from rules.json."""
    rules_path = Path(__file__).parent / "rules.json"
    try:
        with rules_path.open() as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"[ServiceHealth] Could not load rules.json: {exc}")
        return {}


_RULES: dict = _load_rules()
_CIRCUITS: dict[str, ServiceFallback] = {}


def _get_rule_for_processor(processor_name: str) -> Optional[str]:
    """Map processor name to rule name."""
    return PROCESSOR_RULE_MAP.get(processor_name.lower())


async def _get_or_create_circuit(
    rule: str, config: Optional[dict] = None
) -> ServiceFallback:
    """Get existing ServiceFallback circuit or create new one."""
    if rule not in _CIRCUITS:
        rule_config = config if config is not None else _RULES.get(rule, {})
        _CIRCUITS[rule] = ServiceFallback(
            ServiceFallbackConfig(
                service_name=rule,
                key_prefix="health",  # Use circuit: prefix instead of fallback:
                failure_threshold=rule_config.get("threshold_count", 10),
                failure_window_secs=rule_config.get("window_minutes", 5) * 60,
                fallback_duration_secs=await SERVICE_HEALTH_AUTO_RESUME_MINUTES() * 60,
                fallback_provider_name="paused",
                on_failure_alert=None,
                on_trip_alert=_on_trip_alert,
                on_reset_alert=_on_reset_alert,
            )
        )
    return _CIRCUITS[rule]


# Alert callbacks
async def _on_trip_alert(**kwargs) -> None:
    """Alert when circuit opens (calls paused)."""
    try:
        await _slack_alert.send(
            title="🚨 Service Health: Calls Auto-Paused",
            fields=[
                {"name": "Rule", "value": kwargs.get("service_name", "unknown")},
                {"name": "Status", "value": "Outbound calls are now paused"},
            ],
        )
    except Exception as e:
        logger.warning(f"[ServiceHealth] Trip alert failed: {e}")


async def _on_reset_alert(**kwargs) -> None:
    """Alert when circuit closes (calls resumed)."""
    try:
        await _slack_alert.send(
            title="✅ Service Health: Calls Resumed",
            fields=[
                {"name": "Rule", "value": kwargs.get("service_name", "unknown")},
                {"name": "Status", "value": "Outbound calls have resumed"},
            ],
        )
    except Exception as e:
        logger.warning(f"[ServiceHealth] Reset alert failed: {e}")


class ServiceHealthMonitor:
    """Service health monitor using ServiceFallback (reused from STT fallback)."""

    async def record_pipeline_error(
        self,
        processor: str,
        error: str,
        call_sid: str = "",
        context: str = "mid-call",
    ) -> bool:
        """Record a pipeline error. Called from on_pipeline_error handler."""
        if not await ENABLE_SERVICE_HEALTH_MONITORING():
            return False

        rule = _get_rule_for_processor(processor)
        if not rule:
            return False

        circuit = await _get_or_create_circuit(rule)
        return await circuit.record_failure(
            error_msg=error,
            call_sid=call_sid,
            context=context,
        )

    async def is_globally_paused(self) -> bool:
        """Check if any circuit is open (global pause active)."""
        for circuit in _CIRCUITS.values():
            if await circuit.is_active():
                return True
        return False

    async def pause_calls(
        self, reason: str, paused_by: str = "auto", source_rule: Optional[str] = None
    ) -> None:
        """Manually open a circuit (dashboard/API)."""
        rule = source_rule or "manual"
        # Use existing rule config if available, otherwise use default for manual pause
        config = (
            _RULES.get(rule)
            if rule in _RULES
            else {"threshold_count": 1, "window_minutes": 1}
        )
        circuit = await _get_or_create_circuit(rule, config)
        await circuit._activate(await get_redis_service())

    async def resume_calls(self, resumed_by: str = "auto") -> None:
        """Manually close all circuits (dashboard/API)."""
        for circuit in _CIRCUITS.values():
            await circuit.reset_to_primary()

    async def get_status(self) -> dict:
        """Get current status of all circuits."""
        open_circuits = []
        for rule, circuit in _CIRCUITS.items():
            if await circuit.is_active():
                open_circuits.append(rule)
        return {
            "is_paused": len(open_circuits) > 0,
            "open_circuits": open_circuits,
        }

    async def run_auto_health_check(self) -> None:
        """Evaluate all circuits and auto-reset if clean."""
        if not await ENABLE_SERVICE_HEALTH_MONITORING():
            return

        for rule, circuit in _CIRCUITS.items():
            if not await circuit.is_active():
                continue
            # Check if clean (no recent failures) - ServiceFallback TTL handles this
            await circuit.reset_to_primary()


# Background task registration
async def check_and_reset_circuits() -> None:
    """Background task to check and reset circuits."""
    monitor = ServiceHealthMonitor()
    await monitor.run_auto_health_check()


async def initialize_service_health_tasks(scheduler: BackgroundTaskScheduler) -> None:
    """Register service health check task if enabled."""
    if not await ENABLE_SERVICE_HEALTH_MONITORING():
        logger.info("[ServiceHealth] Monitoring disabled")
        return

    scheduler.register_task(
        name="service_health_check",
        func=check_and_reset_circuits,
        interval_seconds=60,
    )
    logger.info("[ServiceHealth] Registered health check task")


# Singleton instance
service_health_monitor = ServiceHealthMonitor()
