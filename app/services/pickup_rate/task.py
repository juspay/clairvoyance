"""
Pickup Rate Alert Background Task Initialization

Registers the pickup_rate_monitor tasks with the BackgroundTaskScheduler.
Mirrors the pattern used by app/services/langfuse/tasks/task.py.

Two tasks are registered when conditions are met:
  - pickup_rate_monitor            → global alert (gated by ENABLE_PICKUP_RATE_ALERT)
  - pickup_rate_monitor_merchants  → per-merchant alerts (merchants table config)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config.dynamic import (
    ENABLE_PICKUP_RATE_ALERT,
    PICKUP_RATE_ALERT_INTERVAL_SECONDS,
)
from app.core.config.static import SLACK_WEBHOOK_URL
from app.core.logger import logger
from app.services.pickup_rate.monitor import pickup_rate_monitor

if TYPE_CHECKING:
    from app.core.background_tasks.scheduler import BackgroundTaskScheduler


async def initialize_pickup_rate_tasks(scheduler: "BackgroundTaskScheduler") -> bool:
    """
    Register pickup rate alert tasks if all required configuration is present.

    Registers two independent tasks:
    1. pickup_rate_monitor            - global alert, gated by ENABLE_PICKUP_RATE_ALERT
    2. pickup_rate_monitor_merchants  - per-merchant alerts, always registered when
                                        SLACK_WEBHOOK_URL is set (individual merchants
                                        opt in via their pickup_rate_alert_enabled flag)

    Args:
        scheduler: BackgroundTaskScheduler instance to register the task with.

    Returns:
        True if at least one task was registered, False if all were skipped.
    """
    if not SLACK_WEBHOOK_URL:
        logger.warning(
            "PickupRateMonitor: SLACK_WEBHOOK_URL not configured - skipping all task registration"
        )
        return False

    interval_seconds = await PICKUP_RATE_ALERT_INTERVAL_SECONDS()
    registered_any = False

    # --- Global alert task ---
    enabled = await ENABLE_PICKUP_RATE_ALERT()
    if enabled:
        try:
            scheduler.register_task(
                name="pickup_rate_monitor",
                func=pickup_rate_monitor.check_and_alert,
                interval_seconds=interval_seconds,
            )
            logger.info(
                f"Registered pickup_rate_monitor (global) background task "
                f"(interval={interval_seconds}s)"
            )
            registered_any = True
        except Exception as e:
            logger.error(f"Failed to register pickup_rate_monitor task: {e}")
    else:
        logger.debug(
            "PickupRateMonitor: ENABLE_PICKUP_RATE_ALERT is false - skipping global task"
        )

    # --- Per-merchant alert task ---
    try:
        scheduler.register_task(
            name="pickup_rate_monitor_merchants",
            func=pickup_rate_monitor.check_all_merchants,
            interval_seconds=interval_seconds,
        )
        logger.info(
            f"Registered pickup_rate_monitor_merchants (per-merchant) background task "
            f"(interval={interval_seconds}s)"
        )
        registered_any = True
    except Exception as e:
        logger.error(f"Failed to register pickup_rate_monitor_merchants task: {e}")

    return registered_any
