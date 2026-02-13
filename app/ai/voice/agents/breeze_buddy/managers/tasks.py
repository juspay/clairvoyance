"""
Breeze Buddy Background Task Initialization

This module provides functionality to initialize Breeze Buddy background tasks,
specifically the call initiation task that processes backlog leads.
"""

from app.ai.voice.agents.breeze_buddy.managers.calls import process_backlog_leads
from app.core.config.dynamic import (
    CALL_INITIATION_INTERVAL_SECONDS,
    ENABLE_BB_CALL_INITIATION_LOOP,
)
from app.core.logger import logger


async def initialize_call_initiation_tasks(scheduler) -> bool:
    """
    Initialize Breeze Buddy call initiation background tasks if properly configured.

    Args:
        scheduler: BackgroundTaskScheduler instance to register tasks with

    Returns:
        bool: True if tasks were registered successfully, False if skipped
    """

    # Check if call initiation loop is enabled (dynamic config from Redis)
    if not await ENABLE_BB_CALL_INITIATION_LOOP():
        logger.debug(
            "Call initiation tasks skipped - ENABLE_BB_CALL_INITIATION_LOOP is disabled"
        )
        return False

    try:
        # Get interval from dynamic config
        interval_seconds = await CALL_INITIATION_INTERVAL_SECONDS()

        # Validate interval to prevent hot loops
        if not interval_seconds or interval_seconds <= 0:
            logger.error(
                f"Invalid CALL_INITIATION_INTERVAL_SECONDS value: {interval_seconds}. "
                "Must be a positive integer. Skipping task registration."
            )
            return False

        # Register call initiation task
        scheduler.register_task(
            name="breeze_buddy_call_initiation",
            func=process_backlog_leads,
            interval_seconds=interval_seconds,
        )
        logger.info(
            f"Registered breeze_buddy_call_initiation background task "
            f"(interval: {interval_seconds}s)"
        )

        return True

    except Exception as e:
        logger.error(f"Failed to register call initiation background tasks: {e}")
        return False
