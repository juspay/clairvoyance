"""
Voice Agent Process Pool Manager

Primary Objective:
This module is designed to eliminate the ~5-6 second latency associated with
initializing a new Python process and loading all the necessary AI models (STT, VAD, etc.)
for the voice agent.

How it Works:
It maintains a pool of pre-warmed, running voice agent subprocesses. These processes
have already completed their expensive initialization and are waiting idly for a session
assignment. When a new user connects, a process is instantly allocated from the pool
and configured for the specific session via its `stdin`. This moves the costly
initialization out of the user-facing request path.

Key Features:
- Manages a queue of available, pre-warmed voice agent processes.
- Tracks processes that are currently in active sessions.
- Automatically replenishes the pool in the background as processes are used.
- Monitors the health of pooled processes and replaces unhealthy ones.
- Provides a graceful fallback to create processes on-demand if the pool is exhausted.
- Handles all inter-process communication (IPC) for session configuration.
"""

import asyncio
import os
import uuid
from asyncio import Queue
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from app.core.logger import logger


class VoiceAgentProcess:
    """
    A data class representing a single, running voice agent subprocess.

    This object holds the process handle and its associated state, such as whether it's
    busy, which session it's handling, and whether it's a permanent (managed) part of
    the pool or a temporary (ephemeral) one created as a fallback.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        process_id: str,
        *,
        is_managed: bool = True,
    ):
        self.process = process
        self.process_id = process_id
        self.is_busy = False
        self.session_id: Optional[str] = None
        self.created_at = datetime.now()
        self.last_used_at: Optional[datetime] = None
        self.is_managed = is_managed  # Managed = part of the permanent pool
        self.monitor_task: Optional[asyncio.Task] = None

    def mark_busy(self, session_id: str):
        """Marks the process as in-use for a specific session."""
        self.is_busy = True
        self.session_id = session_id
        self.last_used_at = datetime.now()

    def mark_available(self):
        """Marks the process as free to handle a new session."""
        self.is_busy = False
        self.session_id = None

    def is_healthy(self) -> bool:
        """
        Checks if the subprocess is still running.
        A process is healthy if its return code is `None`.
        """
        return self.process.returncode is None


class VoiceAgentPool:
    """
    Manages the entire lifecycle of pre-warmed voice agent processes.

    This class is the core of the process pooling mechanism. It handles initialization,
    creation, allocation, health monitoring, and cleanup of all subprocesses.
    """

    def __init__(self, pool_size: int = 3, max_pool_size: int = 3):
        """
        Initializes the VoiceAgentPool instance.

        Parameters:
        - pool_size: The target number of available processes to maintain.
        - max_pool_size: The absolute maximum number of managed processes allowed.
        """
        self.pool_size = pool_size
        self.max_pool_size = max_pool_size

        # Queue for ready-to-use, pre-warmed processes.
        self.available_processes: Queue[VoiceAgentProcess] = Queue()
        # Dictionary to track processes currently handling active sessions.
        self.active_processes: Dict[str, VoiceAgentProcess] = {}
        # Dictionaries to track all created processes, separating permanent pool
        # processes from temporary fallback ones.
        self.managed_processes: Dict[str, VoiceAgentProcess] = {}
        self.ephemeral_processes: Dict[str, VoiceAgentProcess] = {}

        # A lock to prevent race conditions during background process creation.
        self._create_lock = asyncio.Lock()
        self.is_creating_process = False

        # Callbacks set by `app/main.py` to break circular dependencies.
        self.room_cleanup_callback = None
        self.session_cleanup_callback = None

        if self.pool_size > self.max_pool_size:
            logger.warning(
                "Voice agent pool size %s exceeds max %s; clamping to max",
                self.pool_size,
                self.max_pool_size,
            )
            self.pool_size = self.max_pool_size

    async def initialize(self):
        """
        Creates the initial set of processes to populate the pool at application startup.

        Flow:
        1. Loops `pool_size` times.
        2. In each iteration, calls `_create_and_add_process()` to launch a new
           subprocess, wait for it to become ready, and add it to the pool.
        """
        logger.info(f"Initializing voice agent pool with {self.pool_size} processes")

        for i in range(self.pool_size):
            try:
                await self._create_and_add_process()
                logger.info(f"Created process {i+1}/{self.pool_size}")
            except Exception as e:
                logger.error(f"Failed to create process {i+1}: {e}")

        logger.info(
            f"Voice agent pool initialized with {self._managed_process_count()} processes"
        )

    def _managed_process_count(self) -> int:
        """Helper to get the current count of permanent (managed) pool processes."""
        return len(self.managed_processes)

    async def _create_and_add_process(self):
        """
        Creates a new pre-warmed voice agent process and adds it to the pool.

        Flow:
        1. Generates a unique ID for the new process.
        2. Constructs the shell command to run the agent in `--pool-mode`.
        3. Uses `asyncio.create_subprocess_shell` to launch the agent. `stdin`, `stdout`,
           and `stderr` are piped for communication and monitoring.
        4. Wraps the process in a `VoiceAgentProcess` object and tracks it.
        5. Waits for the subprocess to print "READY" to its `stdout`, confirming it has
           finished initializing.
        6. Starts a background task (`_monitor_process_output`) to listen for signals
           from the subprocess.
        7. Adds the now-ready process to the `available_processes` queue.
        """
        process_id = str(uuid.uuid4())

        try:
            cmd = f"python3 -u -m app.agents.voice.automatic --pool-mode --process-id {process_id}"

            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=Path(__file__).parent.parent.parent.parent,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Merge stderr for easier logging
                env=os.environ,
            )

            voice_process = VoiceAgentProcess(proc, process_id, is_managed=True)
            self.managed_processes[process_id] = voice_process

            await self._wait_for_process_ready(voice_process)

            # Create and track the background monitoring task
            voice_process.monitor_task = asyncio.create_task(
                self._monitor_process_output(voice_process)
            )

            await self.available_processes.put(voice_process)

            logger.info(f"Created and added process {process_id} to pool")

        except Exception as e:
            logger.error(f"Failed to create process {process_id}: {e}")
            if process_id in self.managed_processes:
                await self._cleanup_process(process_id)
            raise

    async def _wait_for_process_ready(
        self, voice_process: VoiceAgentProcess, timeout: int = 30
    ):
        """
        Waits for a subprocess to signal that it's initialized and ready.

        Implementation:
        - Reads the process's `stdout` line by line with a short timeout.
        - When it reads a line containing "READY", it returns successfully.
        - If the process dies during startup or the overall timeout is reached,
          it raises an error.
        """
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            if not voice_process.is_healthy():
                raise RuntimeError(
                    f"Process {voice_process.process_id} died during startup"
                )

            try:
                ready_line = await asyncio.wait_for(
                    voice_process.process.stdout.readline(), timeout=0.5
                )

                if ready_line:
                    ready_line = ready_line.decode("utf-8").strip()
                    logger.debug(
                        f"Process {voice_process.process_id} output: {ready_line}"
                    )
                    if "READY" in ready_line:
                        logger.info(
                            f"Process {voice_process.process_id} signaled READY"
                        )
                        return

            except asyncio.TimeoutError:
                # No output received in the last 0.5s, continue the loop to try again.
                continue

        raise TimeoutError(
            f"Process {voice_process.process_id} timed out waiting for READY signal"
        )

    async def _monitor_process_output(self, voice_process: VoiceAgentProcess):
        """
        Monitors a subprocess's output in a background task.

        Primary Purpose:
        - Listens for the "SESSION_ENDED" signal, which the subprocess prints after
          it finishes handling a call.
        - When this signal is received, it triggers the logic to return the process
          to the available pool (`_return_process_to_pool`).
        - In development environments, it also forwards all subprocess logs to the
          main application's logger for easier debugging.
        """
        from app.core import config

        is_dev = config.ENVIRONMENT.lower() in ["dev", "development"]

        try:
            while voice_process.is_healthy():
                try:
                    line = await asyncio.wait_for(
                        voice_process.process.stdout.readline(), timeout=2.0
                    )
                    if line:
                        line = line.decode("utf-8").strip()

                        if "SESSION_ENDED" in line:
                            logger.info(
                                f"Process {voice_process.process_id[:8]} session ended, returning to pool"
                            )
                            await self._return_process_to_pool(voice_process)
                        elif is_dev and line:
                            logger.info(
                                f"[Process {voice_process.process_id[:8]}] {line}"
                            )

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.debug(
                        f"Process monitoring error for {voice_process.process_id}: {e}"
                    )
                    break
        finally:
            # Best-effort cleanup in case the monitor exits unexpectedly
            logger.debug(
                f"Monitoring stopped for process {voice_process.process_id}. Performing cleanup check."
            )
            # Check if the process is still tracked as active
            active_session_id = next(
                (
                    sid
                    for sid, proc in self.active_processes.items()
                    if proc.process_id == voice_process.process_id
                ),
                None,
            )
            if active_session_id:
                logger.warning(
                    f"Process {voice_process.process_id} monitor exited while still active. Forcing return to pool."
                )
                try:
                    await self._return_process_to_pool(voice_process)
                except Exception as cleanup_error:
                    logger.error(
                        f"Error during best-effort cleanup for process {voice_process.process_id}: {cleanup_error}"
                    )

    async def _return_process_to_pool(self, voice_process: VoiceAgentProcess):
        """
        Handles the logic for returning a process to the pool after its session ends.

        Flow:
        1. Finds the `session_id` associated with the given `voice_process`.
        2. Calls `return_process(session_id)` to perform the actual state change.
        3. Triggers the `room_cleanup_callback` to ensure the associated Daily room
           is also cleaned up immediately.
        """
        try:
            session_id = next(
                (
                    sid
                    for sid, proc in self.active_processes.items()
                    if proc.process_id == voice_process.process_id
                ),
                None,
            )

            if session_id:
                await self.return_process(session_id)
                logger.info(
                    f"Process {voice_process.process_id[:8]} returned to pool automatically"
                )

                await self._trigger_room_cleanup(session_id)

            else:
                logger.warning(
                    f"Could not find session for process {voice_process.process_id[:8]}"
                )
        except Exception as e:
            logger.error(
                f"Error returning process {voice_process.process_id[:8]} to pool: {e}"
            )

    async def _trigger_room_cleanup(self, session_id: str):
        """
        Invokes the cleanup callbacks provided by `app/main.py`.

        This is a crucial part of breaking the circular dependency between the pools
        and the session manager. The pool doesn't know how to clean up rooms or
        sessions directly; it just calls the functions it was given during initialization.
        """
        try:
            if self.room_cleanup_callback:
                await self.room_cleanup_callback(session_id)
                logger.info(f"Triggered room cleanup for session {session_id}")

            if self.session_cleanup_callback:
                await self.session_cleanup_callback(session_id)

        except Exception as e:
            logger.error(f"Error triggering room cleanup for session {session_id}: {e}")

    async def get_process(self, session_id: str) -> VoiceAgentProcess:
        """
        Retrieves an available process from the pool for a new session.

        Flow:
        1. Tries to get a process from the `available_processes` queue.
        2. It loops to ensure it gets a healthy process, discarding any that may have
           died while waiting in the queue.
        3. If successful:
           a. Marks the process as busy and moves it to `active_processes`.
           b. Schedules a background replenishment task if the pool is low.
           c. Returns the allocated process.
        4. If the queue is empty, it calls `_create_process_direct` as a fallback.
        """
        logger.info(f"Getting process for session {session_id}")

        try:
            for _ in range(max(1, self.available_processes.qsize())):
                process = await asyncio.wait_for(
                    self.available_processes.get(), timeout=0.1
                )

                if process.is_healthy():
                    process.mark_busy(session_id)
                    self.active_processes[session_id] = process

                    if (
                        self.available_processes.qsize() == 0
                        and self._managed_process_count() < self.max_pool_size
                        and not self.is_creating_process
                    ):
                        logger.info("Pool getting low, creating background process")
                        asyncio.create_task(self._create_background_process())

                    logger.info(
                        f"Assigned process {process.process_id} to session {session_id}"
                    )
                    return process
                else:
                    logger.warning(
                        f"Process {process.process_id} is unhealthy, cleaning up"
                    )
                    await self._cleanup_process(process.process_id)

        except asyncio.TimeoutError:
            pass

        logger.warning(
            f"Pool exhausted for session {session_id}, creating process directly"
        )
        return await self._create_process_direct(session_id)

    async def _create_background_process(self):
        """
        Safely creates a new process in a background task to replenish the pool.

        Implementation:
        - Uses `_create_lock` to prevent race conditions, ensuring only one
          replenishment task runs at a time.
        """
        async with self._create_lock:
            if self.is_creating_process:
                return
            self.is_creating_process = True

        try:
            if self._managed_process_count() >= self.max_pool_size:
                logger.debug(
                    "Process pool already at max capacity; skipping background creation"
                )
            else:
                await self._create_and_add_process()
                logger.info("Background process created successfully")
        except Exception as e:
            logger.error(f"Failed to create background process: {e}")
        finally:
            async with self._create_lock:
                self.is_creating_process = False

    async def _create_process_direct(self, session_id: str) -> VoiceAgentProcess:
        """
        Creates a process on-demand when the pool is exhausted (fallback).

        Flow:
        1. Launches a new subprocess in the same way as a pooled process.
        2. However, it marks it as `is_managed=False` and tracks it in the
           `ephemeral_processes` dictionary.
        3. This distinction ensures that when the session is over, this temporary
           process is terminated rather than being returned to the pool.
        """
        logger.info(f"Creating direct process for session {session_id}")

        process_id = str(uuid.uuid4())

        try:
            cmd = f"python3 -u -m app.agents.voice.automatic --pool-mode --process-id {process_id}"

            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=Path(__file__).parent.parent.parent.parent,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ,
            )

            voice_process = VoiceAgentProcess(proc, process_id, is_managed=False)
            self.ephemeral_processes[process_id] = voice_process

            await self._wait_for_process_ready(voice_process)

            voice_process.monitor_task = asyncio.create_task(
                self._monitor_process_output(voice_process)
            )

            voice_process.mark_busy(session_id)
            self.active_processes[session_id] = voice_process

            logger.info(f"Direct process {process_id} created for session {session_id}")
            return voice_process

        except Exception as e:
            logger.error(f"Failed to create direct process {process_id}: {e}")
            if process_id in self.ephemeral_processes:
                await self._cleanup_process(process_id)
            raise

    async def return_process(self, session_id: str):
        """
        Returns a process to the pool or terminates it after a session ends.

        Flow:
        1. Removes the process from the `active_processes` dictionary.
        2. Checks the `is_managed` flag:
           a. If `True` (it's a pooled process), it marks it as available and puts
              it back into the `available_processes` queue. If it's unhealthy,
              it's cleaned up and a replacement is scheduled.
           b. If `False` (it's an ephemeral process), it's terminated immediately
              via `_cleanup_process`.
        """
        if session_id not in self.active_processes:
            logger.warning(f"Session {session_id} not found in active processes")
            return

        process = self.active_processes.pop(session_id)

        if process.is_managed:
            if process.is_healthy():
                process.mark_available()
                await self.available_processes.put(process)
                logger.info(f"Returned process {process.process_id} to pool")
            else:
                logger.warning(
                    f"Process {process.process_id} is unhealthy, removing from pool"
                )
                await self._cleanup_process(process.process_id)

                if self._managed_process_count() < self.pool_size or (
                    self.available_processes.qsize() == 0
                    and self._managed_process_count() < self.max_pool_size
                ):
                    asyncio.create_task(self._create_background_process())
        else:
            logger.info(
                f"Ephemeral process {process.process_id[:8]} cleaned up after session {session_id}"
            )
            await self._cleanup_process(process.process_id)
            if (
                self._managed_process_count() < self.pool_size
                and not self.is_creating_process
            ):
                asyncio.create_task(self._create_background_process())

    async def _cleanup_process(self, process_id: str):
        """
        Terminates a subprocess and removes it from all tracking dictionaries.

        Implementation:
        - Gracefully terminates the process with `terminate()` (SIGTERM).
        - Waits for a short period for the process to exit.
        - If it doesn't exit, it forcefully kills it with `kill()` (SIGKILL).
        """
        process = self.managed_processes.pop(
            process_id, None
        ) or self.ephemeral_processes.pop(process_id, None)
        if not process:
            return

        # Cancel the dedicated monitoring task first
        if process.monitor_task and not process.monitor_task.done():
            process.monitor_task.cancel()
            try:
                await process.monitor_task
            except asyncio.CancelledError:
                logger.debug(
                    f"Monitoring task for process {process_id} cancelled successfully."
                )

        try:
            if process.is_healthy():
                process.process.terminate()
                try:
                    await asyncio.wait_for(process.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Process {process_id} did not terminate, killing")
                    process.process.kill()
                    await process.process.wait()

        except Exception as e:
            logger.error(f"Error cleaning up process {process_id}: {e}")

        logger.info(f"Cleaned up process {process_id}")

    async def get_pool_stats(self) -> Dict:
        """Returns a dictionary of current pool statistics for monitoring."""
        managed_active = sum(
            1 for proc in self.active_processes.values() if proc.is_managed
        )
        ephemeral_active = len(self.active_processes) - managed_active

        return {
            "managed_processes": self._managed_process_count(),
            "available_processes": self.available_processes.qsize(),
            "active_processes": len(self.active_processes),
            "managed_active": managed_active,
            "ephemeral_active": ephemeral_active,
            "is_creating_process": self.is_creating_process,
            "pool_size": self.pool_size,
            "max_pool_size": self.max_pool_size,
        }

    async def cleanup(self):
        """
        Cleans up all managed processes during application shutdown.

        Flow:
        - Iterates through all tracked processes (managed and ephemeral).
        - Calls `_cleanup_process` for each one to ensure they are all terminated.
        """
        logger.info("Cleaning up voice agent pool")

        # Clean up all running subprocesses
        all_pids = list(self.managed_processes.keys()) + list(
            self.ephemeral_processes.keys()
        )
        if all_pids:
            logger.info(f"Cleaning up {len(all_pids)} subprocesses...")
            await asyncio.gather(*(self._cleanup_process(pid) for pid in all_pids))

        self.active_processes.clear()
        logger.info("Voice agent pool cleanup complete")


# Global pool instance, managed by the functions below.
voice_agent_pool: Optional[VoiceAgentPool] = None


def get_voice_agent_pool() -> VoiceAgentPool:
    """
    Provides global access to the single VoiceAgentPool instance.

    This function acts as a singleton accessor, preventing circular imports.
    """
    global voice_agent_pool
    if voice_agent_pool is None:
        voice_agent_pool = VoiceAgentPool()
    return voice_agent_pool


async def initialize_voice_agent_pool(pool_size: int = 3, max_pool_size: int = 3):
    """
    Initializes the global VoiceAgentPool instance.

    Called once at application startup from `app/main.py`.
    """
    global voice_agent_pool
    voice_agent_pool = VoiceAgentPool(pool_size=pool_size, max_pool_size=max_pool_size)
    await voice_agent_pool.initialize()


async def cleanup_voice_agent_pool():
    """
    Cleans up the global VoiceAgentPool instance.

    Called once at application shutdown from `app/main.py`.
    """
    global voice_agent_pool
    if voice_agent_pool:
        await voice_agent_pool.cleanup()
        voice_agent_pool = None
