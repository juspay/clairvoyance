"""
Langfuse Background Task Initialization, Score Fetching, and Score Monitoring

This module provides functionality to initialize Langfuse background tasks,
fetch scores from Langfuse via REST API, and monitor for failures.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.static import (
    ENABLE_BB_LANGFUSE_MONITORING_LOOP,
    LANGFUSE_EVALUATORS, 
    SLACK_WEBHOOK_URL,
    SCORE_CHECK_INTERVAL_SECONDS,
)
from app.core.logger import logger
from app.services.langfuse.tasks.score_monitor.score import score_monitor


async def initialize_langfuse_tasks(scheduler) -> bool:
    """
    Initialize all Langfuse background tasks if properly configured.
    
    Args:
        scheduler: BackgroundTaskScheduler instance to register tasks with
        
    Returns:
        bool: True if tasks were registered successfully, False if skipped
    """
    
    # Check if all required configuration is present
    if not (ENABLE_BB_LANGFUSE_MONITORING_LOOP and LANGFUSE_EVALUATORS and SLACK_WEBHOOK_URL):
        logger.debug("Langfuse tasks skipped - missing required configuration")
        return False
        
    try:
        # Register Langfuse score monitoring task
        scheduler.register_task(
            name="langfuse_score_monitor",
            func=score_monitor.check_and_alert,
            interval_seconds=SCORE_CHECK_INTERVAL_SECONDS,
        )
        logger.info("Registered langfuse_score_monitor background task")
        
        # Future Langfuse tasks can be registered here
        # scheduler.register_task(...)
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to register Langfuse background tasks: {e}")
        return False
