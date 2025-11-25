"""
Background task scheduler with distributed locking support.

This module provides a generic framework for running background tasks
across multiple pods with Redis-based distributed locking to ensure
only one pod executes each task at a time.
"""

from app.core.background_tasks.task import BackgroundTask
from app.core.background_tasks.scheduler import BackgroundTaskScheduler

__all__ = [
    "BackgroundTaskScheduler",
    "BackgroundTask",
]
