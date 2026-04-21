"""
Pickup Rate Monitor

Background task that checks whether global (and per-merchant) call pickup rates
have dropped below a configured threshold, and sends a Slack warning alert when
they have.

Edge-case handling:
- Zero calls / leads in window  →  skip (no data to evaluate)
- Redis unavailable             →  fail-open: log warning, allow alert to fire
- DB error in calculator        →  log error, skip this check cycle
- Slack send failure            →  log error, do NOT mark-alerted (will retry next cycle)
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.config.dynamic import (
    ENABLE_PICKUP_RATE_ALERT,
    PICKUP_RATE_ALERT_INTERVAL_SECONDS,
    PICKUP_RATE_ALERT_THRESHOLD,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.merchants import (
    get_merchants_with_pickup_rate_alert_enabled,
)
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
        """Global alert entry point called by BackgroundTaskScheduler on each tick."""
        config = await self.load_config()

        if not config.enabled:
            logger.debug("PickupRateMonitor: alerting disabled, skipping check")
            return

        await self._run_alert_for_config(config)

    async def check_all_merchants(self) -> None:
        """Per-merchant alert entry point called by BackgroundTaskScheduler on each tick.

        Loads all active merchants with pickup_rate_alert_enabled=true and runs
        the alert logic for each one independently. A failure on one merchant
        does not block the others.
        """
        try:
            merchants = await get_merchants_with_pickup_rate_alert_enabled()
        except Exception as e:
            logger.error(
                f"PickupRateMonitor: failed to load merchants for per-merchant alert: {e}"
            )
            return

        if not merchants:
            logger.debug(
                "PickupRateMonitor: no merchants with pickup rate alert enabled"
            )
            return

        interval_seconds = await PICKUP_RATE_ALERT_INTERVAL_SECONDS()
        global_threshold = await PICKUP_RATE_ALERT_THRESHOLD()

        logger.info(
            f"PickupRateMonitor: running per-merchant check for {len(merchants)} merchant(s)"
        )

        for merchant in merchants:
            try:
                threshold = (
                    merchant.pickup_rate_alert_threshold
                    if merchant.pickup_rate_alert_threshold is not None
                    else global_threshold
                )
                config = self._build_alert_config(
                    interval_seconds=interval_seconds,
                    threshold_percent=threshold,
                    scope=f"merchant:{merchant.merchant_id}",
                    merchant_id=merchant.merchant_id,
                )
                await self._run_alert_for_config(config)
            except Exception as e:
                logger.error(
                    f"PickupRateMonitor: error processing merchant "
                    f"'{merchant.merchant_id}': {e}"
                )

    # -----------------------------------------------------------------------
    # Configuration loading
    # -----------------------------------------------------------------------

    def _build_alert_config(
        self,
        interval_seconds: int,
        threshold_percent: float,
        scope: str = "global",
        merchant_id: Optional[str] = None,
        enabled: bool = True,
    ) -> AlertConfig:
        """Build an AlertConfig with shared defaults applied.

        All alert scopes use alert_type='BOTH'. Callers only need to supply
        the fields that differ between global and per-merchant configs.
        """
        return AlertConfig(
            enabled=enabled,
            interval_seconds=interval_seconds,
            threshold_percent=threshold_percent,
            alert_type="BOTH",
            scope=scope,
            merchant_id=merchant_id,
        )

    async def load_config(self) -> AlertConfig:
        """Fetch runtime config from dynamic (Redis/DevCycle) getters."""
        enabled = await ENABLE_PICKUP_RATE_ALERT()
        interval_seconds = await PICKUP_RATE_ALERT_INTERVAL_SECONDS()
        threshold_percent = await PICKUP_RATE_ALERT_THRESHOLD()

        return self._build_alert_config(
            interval_seconds=interval_seconds,
            threshold_percent=threshold_percent,
            enabled=enabled,
            scope="global",
        )

    # -----------------------------------------------------------------------
    # Core alert logic (shared by global and per-merchant paths)
    # -----------------------------------------------------------------------

    async def _run_alert_for_config(self, config: AlertConfig) -> None:
        """Run a full alert cycle for a given AlertConfig scope."""
        now = datetime.now(timezone.utc)
        start_date = now - timedelta(hours=config.lookback_hours)

        logger.info(
            f"PickupRateMonitor: checking pickup rate for scope='{config.scope}' "
            f"[{start_date.isoformat()} → {now.isoformat()}] "
            f"threshold={config.threshold_percent}% type={config.alert_type}"
        )

        rates = await compute_pickup_rates(
            start_date=start_date,
            end_date=now,
            merchant_id=config.merchant_id,
        )

        if rates is None:
            logger.error(
                f"PickupRateMonitor: DB query failed for scope='{config.scope}', "
                "skipping alert cycle"
            )
            return

        # Skip when there is no data - avoids false alerts on idle systems
        if rates["calls_attempted"] == 0 and rates["total_leads"] == 0:
            logger.info(
                f"PickupRateMonitor: no calls or leads in window for "
                f"scope='{config.scope}', skipping alert"
            )
            return

        if not self._is_threshold_breached(rates, config):
            logger.info(
                f"PickupRateMonitor: pickup rate OK for scope='{config.scope}' "
                f"(call={rates['call_pickup_rate']}% "
                f"lead={rates['lead_pickup_rate']}% "
                f"threshold={config.threshold_percent}%)"
            )
            return

        # Dedup check - skip if we alerted too recently
        if not await self._should_alert(
            config.redis_dedup_key, config.interval_seconds
        ):
            logger.info(
                f"PickupRateMonitor: threshold breached for scope='{config.scope}' "
                f"but already alerted recently (key={config.redis_dedup_key})"
            )
            return

        # ------------------------------------------------------------------
        # Build and send Slack alert
        # ------------------------------------------------------------------
        display_date = datetime.now(timezone.utc).strftime("%d-%m-%y")
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
                        f"• Rate: *{rates['call_pickup_rate']}%* "
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
                        f"• Rate: *{rates['lead_pickup_rate']}%* "
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
            f"Low Pickup Rate Alert [{config.scope}]: "
            f"call={rates['call_pickup_rate']}% "
            f"lead={rates['lead_pickup_rate']}% threshold={threshold}%"
        )

        try:
            success = await slack_alert.send(
                title=title,
                fields=fields,
                sections=sections,
                fallback_text=fallback,
            )
        except Exception as e:
            logger.error(
                f"PickupRateMonitor: exception while sending Slack alert "
                f"for scope='{config.scope}': {e}"
            )
            success = False

        if success:
            logger.info(
                f"PickupRateMonitor: alert sent for scope='{config.scope}' "
                f"call_rate={rates['call_pickup_rate']}% "
                f"lead_rate={rates['lead_pickup_rate']}%"
            )
            await self._mark_alerted(config.redis_dedup_key, config.interval_seconds)
        else:
            logger.warning(
                f"PickupRateMonitor: Slack send failed for scope='{config.scope}' "
                "- will retry on next cycle"
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

        if alert_type == "CALL_BASED":
            if rates["calls_attempted"] == 0:
                return False
            return call_breached

        if alert_type == "LEAD_BASED":
            if rates["total_leads"] == 0:
                return False
            return lead_breached

        # Default: "BOTH" - alert if *either* rate is below threshold
        call_ok = rates["calls_attempted"] == 0 or not call_breached
        lead_ok = rates["total_leads"] == 0 or not lead_breached
        return not (call_ok and lead_ok)

    # -----------------------------------------------------------------------
    # Redis dedup helpers
    # -----------------------------------------------------------------------

    async def _should_alert(self, redis_key: str, interval_seconds: int) -> bool:
        """Return True when it is safe to send an alert (dedup TTL has expired).

        Fails open: if Redis is unavailable the alert is allowed to fire so
        we do not silently drop warnings on infrastructure issues.
        """
        if not is_redis_configured():
            logger.warning(
                "PickupRateMonitor: Redis not configured - skipping dedup check, "
                "proceeding with alert"
            )
            return True

        try:
            redis_service = await get_redis_service()
            key_exists = await redis_service.exists(redis_key)
            return not key_exists
        except Exception as e:
            logger.warning(
                f"PickupRateMonitor: Redis dedup check failed ({e}) - "
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
