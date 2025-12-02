"""
Generic background task scheduler with distributed locking.

Provides a reusable scheduler that runs background tasks periodically
with Redis-based distributed locking to ensure only one pod executes
each task at a time.
"""

import asyncio
import uuid
from typing import Callable, Dict, Optional

from app.core.background_tasks.task import BackgroundTask
from app.core.logger import logger
from app.services.redis import get_redis_service


class BackgroundTaskScheduler:
    """
    Scheduler for running background tasks with distributed locking.

    The scheduler runs a loop every minute in all pods, but uses distributed
    locking to ensure only one pod executes each task. Tasks can have different
    execution intervals (e.g., every 1 minute, every 5 minutes, etc.).

    Example:
        ```python
        scheduler = BackgroundTaskScheduler()

        # Register tasks
        scheduler.register_task(
            name="langfuse_score_monitor",
            func=score_monitor.check_and_alert,
            interval_seconds=600,  # Every 10 minutes
            lock_key="langfuse:score_monitor:lock"
        )

        scheduler.register_task(
            name="cleanup_old_sessions",
            func=cleanup_sessions,
            interval_seconds=300,  # Every 5 minutes
            lock_key="background:cleanup_sessions:lock"
        )

        # Start scheduler
        asyncio.create_task(scheduler.start())
        ```
    """

    def __init__(self, loop_interval_seconds: int = 60):
        """
        Initialize the background task scheduler.

        Args:
            loop_interval_seconds: How often the main loop runs (default: 60 seconds)
        """
        self.loop_interval_seconds = loop_interval_seconds
        self.tasks: Dict[str, BackgroundTask] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register_task(
        self,
        name: str,
        func: Callable,
        interval_seconds: int,
    ) -> str:
        """
        Register a new background task.

        Args:
            name: Human-readable name for the task (for logging/debugging)
                 This will be used to generate the Redis lock key
            func: Async function to execute
            interval_seconds: How often to run the task

        Returns:
            Unique task ID (UUID) for the registered task
        """
        # Validate that task name is unique
        for existing_task in self.tasks.values():
            if existing_task.name == name:
                raise ValueError(
                    f"Task with name '{name}' already registered. "
                    f"Each task must have a unique name to prevent conflicts in multi-pod scenarios."
                )

        # Generate unique ID for the task
        task_id = str(uuid.uuid4())

        task = BackgroundTask(
            name=name,
            func=func,
            interval_seconds=interval_seconds,
        )

        self.tasks[task_id] = task

        # Generate lock key from task name for logging
        lock_key = f"background:task:{name.lower().replace(' ', '_')}:lock"
        logger.info(
            f"Registered background task '{name}' with ID {task_id} "
            f"(interval: {interval_seconds}s, lock_key: {lock_key})"
        )

        return task_id

    async def _execute_task(self, task: BackgroundTask) -> None:
        """
        Execute a single task with distributed locking.

        The Redis lock's TTL determines when the task is due to run again.
        All pods attempt to acquire the lock, but only one succeeds.

        Args:
            task: The task to execute
        """
        # Generate lock key from task name
        lock_key = f"background:task:{task.name.lower().replace(' ', '_')}:lock"

        # Try to acquire distributed lock using Redis SET NX EX
        try:
            redis_service = await get_redis_service()
            lock_acquired = await redis_service.set(
                key=lock_key,
                value="locked",
                nx=True,
                ex=task.interval_seconds,
            )
        except Exception as e:
            logger.error(f"Redis error for task '{task.name}': {e}. Skipping task.")
            return

        if not lock_acquired:
            logger.debug(f"Task '{task.name}' lock already held, skipping...")
            return

        logger.info(f"Acquired lock for task '{task.name}', executing...")

        # Execute the task
        try:
            await task.func()
            logger.info(f"Task '{task.name}' completed successfully")
        except Exception as e:
            logger.error(f"Error executing task '{task.name}': {e}", exc_info=True)

    async def _run_loop(self) -> None:
        """Main scheduler loop that runs every minute."""
        logger.info(
            f"Background task scheduler started "
            f"(loop interval: {self.loop_interval_seconds}s, "
            f"registered tasks: {len(self.tasks)})"
        )

        while self._running:
            try:
                # Execute all tasks
                for task in self.tasks.values():
                    try:
                        await self._execute_task(task)
                    except Exception as e:
                        logger.error(
                            f"Error in task '{task.name}': {e}",
                            exc_info=True,
                        )

                # Wait for next loop iteration
                await asyncio.sleep(self.loop_interval_seconds)

            except asyncio.CancelledError:
                logger.info("Background task scheduler cancelled, shutting down...")
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                # Wait before retrying on error
                await asyncio.sleep(60)

        logger.info("Background task scheduler stopped")

    async def start(self) -> None:
        """Start the background task scheduler."""
        if self._running:
            logger.warning("Background task scheduler is already running")
            return

        if not self.tasks:
            logger.warning("No tasks registered, scheduler will not start")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Background task scheduler starting...")

    async def stop(self) -> None:
        """Stop the background task scheduler."""
        if not self._running:
            return

        logger.info("Stopping background task scheduler...")
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("Background task scheduler stopped successfully")
