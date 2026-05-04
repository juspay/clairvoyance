"""
Pickup Rate Alert Background Task Initialization

Registers the pickup_rate_monitor task with the BackgroundTaskScheduler.
Mirrors the pattern used by app/services/langfuse/tasks/task.py.
"""

from app.core.config.dynamic import PICKUP_RATE_ALERT_INTERVAL_SECONDS
from app.core.config.static import SLACK_WEBHOOK_URL
from app.core.logger import logger
from app.services.pickup_rate.monitor import pickup_rate_monitor


async def initialize_pickup_rate_tasks(scheduler) -> bool:
    """
    Register pickup rate alert task if required configuration is present.

    The task is always registered when SLACK_WEBHOOK_URL is configured.
    ENABLE_PICKUP_RATE_ALERT is re-read on each scheduler tick inside
    check_and_alert(), so alerts can be enabled or disabled at runtime via
    Redis/DevCycle without a restart.

    PICKUP_RATE_ALERT_INTERVAL_SECONDS is read at registration time and
    determines the scheduler cadence. Changing it in Redis will not affect
    an already-registered task until the process restarts.

    Args:
        scheduler: BackgroundTaskScheduler instance to register the task with.

    Returns:
        True if the task was registered, False if skipped.
    """
    if not SLACK_WEBHOOK_URL:
        logger.warning(
            "PickupRateMonitor: SLACK_WEBHOOK_URL not configured – skipping task registration"
        )
        return False

    interval_seconds = await PICKUP_RATE_ALERT_INTERVAL_SECONDS()

    try:
        scheduler.register_task(
            name="pickup_rate_monitor",
            func=pickup_rate_monitor.check_and_alert,
            interval_seconds=interval_seconds,
        )
        logger.info(
            f"Registered pickup_rate_monitor background task "
            f"(interval={interval_seconds}s)"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to register pickup_rate_monitor task: {e}")
        return False
