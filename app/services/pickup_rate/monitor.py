"""
Pickup Rate Monitor

Background task that checks whether global (and, in Phase 2, per-merchant)
call pickup rates have dropped below a configured threshold, and sends a
Slack warning alert when they have.

Edge-case handling:
- Zero calls / leads in window  →  skip (no data to evaluate)
- Redis unavailable             →  local dedup/mark operations fail open (log warning,
                                   proceed with alert); scheduled execution still
                                   requires BackgroundTaskScheduler Redis lock
- DB error in calculator        →  log error, skip this check cycle
- Slack send failure            →  log error, do NOT mark-alerted (will retry next cycle)
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.core.config.dynamic import (
    ENABLE_PICKUP_RATE_ALERT,
    PICKUP_RATE_ALERT_INTERVAL_SECONDS,
    PICKUP_RATE_ALERT_THRESHOLD,
)
from app.core.logger import logger
from app.services.pickup_rate.calculator import compute_pickup_rates
from app.services.pickup_rate.config import AlertConfig
from app.services.redis import get_redis_service, is_redis_configured
from app.services.slack.alert import slack_alert


class PickupRateMonitor:
    """Monitors pickup rates and fires Slack alerts when thresholds are breached."""

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def check_and_alert(self) -> None:
        """Entry point called by BackgroundTaskScheduler on each tick."""
        config = await self.load_config()

        if not config.enabled:
            logger.debug("PickupRateMonitor: alerting disabled, skipping check")
            return

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(hours=config.lookback_hours)

        logger.info(
            f"PickupRateMonitor: checking pickup rate "
            f"[{start_date.isoformat()} → {now.isoformat()}] "
            f"threshold={config.threshold_percent}% type={config.alert_type}"
        )

        rates = await compute_pickup_rates(
            start_date=start_date,
            end_date=now,
            reseller_id=config.reseller_id,
            merchant_id=config.merchant_id,
        )

        if rates is None:
            logger.error("PickupRateMonitor: DB query failed, skipping alert cycle")
            return

        # Skip when there is no data – avoids false alerts on idle systems
        if rates["calls_attempted"] == 0 and rates["total_leads"] == 0:
            logger.info(
                "PickupRateMonitor: no calls or leads in window, skipping alert"
            )
            return

        if not self._is_threshold_breached(rates, config):
            logger.info(
                f"PickupRateMonitor: pickup rate OK "
                f"(call={rates['call_pickup_rate']:.1f}% "
                f"lead={rates['lead_pickup_rate']:.1f}% "
                f"threshold={config.threshold_percent}%)"
            )
            return

        # Dedup check – skip if we alerted too recently
        if not await self._should_alert(config.redis_dedup_key):
            logger.info(
                f"PickupRateMonitor: threshold breached but already alerted recently "
                f"(key={config.redis_dedup_key})"
            )
            return

        # ------------------------------------------------------------------
        # Build and send Slack alert directly
        # ------------------------------------------------------------------
        display_date = now.strftime("%d-%m-%y")
        title = f"⚠️ Low Pickup Rate Alert - {display_date}"
        threshold = config.threshold_percent

        fields = [
            {"name": "Scope", "value": config.scope.replace("_", " ").title()},
            {"name": "Time Window", "value": f"Last {config.lookback_hours} hours"},
            {"name": "Threshold", "value": f"{threshold}%"},
            {"name": "Alert Type", "value": config.alert_type},
        ]

        sections = []

        if rates["calls_attempted"] > 0:
            call_status = (
                "⚠️ BELOW THRESHOLD"
                if rates["call_pickup_rate"] < threshold
                else "✅ OK"
            )
            sections.append(
                {
                    "title": "Call-Based Pickup Rate",
                    "text": (
                        f"• Rate: *{rates['call_pickup_rate']:.1f}%* "
                        f"(threshold: {threshold}%) {call_status}\n"
                        f"• Calls Attempted: *{rates['calls_attempted']}*\n"
                        f"• Calls Picked Up: *{rates['calls_picked']}*\n"
                        f"• Calls No Answer: *{rates['calls_no_answer']}*"
                    ),
                }
            )
        else:
            sections.append(
                {
                    "title": "Call-Based Pickup Rate",
                    "text": "• No calls attempted in this window",
                }
            )

        if rates["total_leads"] > 0:
            lead_status = (
                "⚠️ BELOW THRESHOLD"
                if rates["lead_pickup_rate"] < threshold
                else "✅ OK"
            )
            sections.append(
                {
                    "title": "Lead-Based Pickup Rate",
                    "text": (
                        f"• Rate: *{rates['lead_pickup_rate']:.1f}%* "
                        f"(threshold: {threshold}%) {lead_status}\n"
                        f"• Total Leads: *{rates['total_leads']}*\n"
                        f"• Leads Picked: *{rates['leads_picked']}*"
                    ),
                }
            )
        else:
            sections.append(
                {
                    "title": "Lead-Based Pickup Rate",
                    "text": "• No leads processed in this window",
                }
            )

        fallback = (
            f"Low Pickup Rate Alert: call={rates['call_pickup_rate']:.1f}% "
            f"lead={rates['lead_pickup_rate']:.1f}% threshold={threshold}%"
        )

        try:
            success = await slack_alert.send(
                title=title,
                fields=fields,
                sections=sections,
                fallback_text=fallback,
            )
        except Exception as e:
            logger.error(f"PickupRateMonitor: exception while sending Slack alert: {e}")
            success = False

        # Only mark-alerted after a successful send so we retry on Slack failure
        if success:
            logger.info(
                f"PickupRateMonitor: alert sent for scope='{config.scope}' "
                f"call_rate={rates['call_pickup_rate']:.1f}% "
                f"lead_rate={rates['lead_pickup_rate']:.1f}%"
            )
            await self._mark_alerted(config.redis_dedup_key, config.interval_seconds)
        else:
            logger.warning(
                "PickupRateMonitor: Slack send failed – will retry on next cycle"
            )

    # -----------------------------------------------------------------------
    # Configuration loading
    # -----------------------------------------------------------------------

    async def load_config(self) -> AlertConfig:
        """Fetch runtime config from dynamic (Redis/DevCycle) getters."""
        enabled = await ENABLE_PICKUP_RATE_ALERT()
        interval_seconds = await PICKUP_RATE_ALERT_INTERVAL_SECONDS()
        threshold_percent = await PICKUP_RATE_ALERT_THRESHOLD()

        return AlertConfig(
            enabled=enabled,
            interval_seconds=interval_seconds,
            threshold_percent=threshold_percent,
            alert_type="BOTH",
            scope="global",
            lookback_hours=max(1, interval_seconds // 3600),
        )

    # -----------------------------------------------------------------------
    # Threshold evaluation
    # -----------------------------------------------------------------------

    def _is_threshold_breached(
        self, rates: Dict[str, Any], config: AlertConfig
    ) -> bool:
        """Return True when the configured rate(s) fall below the threshold."""
        threshold = config.threshold_percent
        alert_type = config.alert_type.upper()

        call_breached = rates["call_pickup_rate"] < threshold
        lead_breached = rates["lead_pickup_rate"] < threshold

        # Only evaluate the relevant rate(s) depending on alert_type
        if alert_type == "CALL_BASED":
            # Skip lead check when no call data, but still evaluate call rate
            if rates["calls_attempted"] == 0:
                return False
            return call_breached

        if alert_type == "LEAD_BASED":
            if rates["total_leads"] == 0:
                return False
            return lead_breached

        # Default: "BOTH" – alert if *either* rate is below threshold
        # Guard against checking a rate with zero denominator
        call_ok = rates["calls_attempted"] == 0 or not call_breached
        lead_ok = rates["total_leads"] == 0 or not lead_breached
        return not (call_ok and lead_ok)

    # -----------------------------------------------------------------------
    # Redis dedup helpers
    # -----------------------------------------------------------------------

    async def _should_alert(self, redis_key: str) -> bool:
        """Return True when it is safe to send an alert (dedup TTL has expired).

        Fails open: if Redis is unavailable the alert is allowed to fire so
        we do not silently drop warnings on infrastructure issues.
        """
        if not is_redis_configured():
            logger.warning(
                "PickupRateMonitor: Redis not configured – skipping dedup check, "
                "proceeding with alert"
            )
            return True

        try:
            redis_service = await get_redis_service()
            key_exists = await redis_service.exists(redis_key)
            return not key_exists
        except Exception as e:
            logger.warning(
                f"PickupRateMonitor: Redis dedup check failed ({e}) – "
                "proceeding with alert (fail-open)"
            )
            return True

    async def _mark_alerted(self, redis_key: str, interval_seconds: int) -> None:
        """Set the Redis dedup key with TTL = interval_seconds."""
        if not is_redis_configured():
            return

        try:
            redis_service = await get_redis_service()
            await redis_service.setex(redis_key, "1", interval_seconds)
            logger.debug(
                f"PickupRateMonitor: marked alerted (key={redis_key} ttl={interval_seconds}s)"
            )
        except Exception as e:
            logger.warning(f"PickupRateMonitor: failed to mark alerted in Redis: {e}")


# Module-level singleton (mirrors score_monitor pattern)
pickup_rate_monitor = PickupRateMonitor()
