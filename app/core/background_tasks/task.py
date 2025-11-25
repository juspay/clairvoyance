"""
Data models for background task scheduler.
"""

import inspect
from dataclasses import dataclass
from typing import Callable


@dataclass
class BackgroundTask:
    """
    Represents a background task that runs periodically with distributed locking.

    The Redis lock key is automatically generated from the task name.

    Attributes:
        name: Unique identifier for the task (e.g., "langfuse_score_monitor")
              Used to generate Redis lock key: background:task:{name}:lock
        func: Async function to execute
        interval_seconds: How often to run the task (e.g., 60 for every minute)
    """

    name: str
    func: Callable
    interval_seconds: int

    def __post_init__(self):
        """Validate task configuration."""
        if not self.name:
            raise ValueError("Task name cannot be empty")
        if not callable(self.func):
            raise ValueError(f"Task '{self.name}' func must be callable")
        if not inspect.iscoroutinefunction(self.func):
            raise ValueError(
                f"Task '{self.name}' func must be an async function (coroutine). "
                f"Use 'async def' to define the function."
            )
        if self.interval_seconds <= 0:
            raise ValueError(f"Task '{self.name}' interval_seconds must be positive")
