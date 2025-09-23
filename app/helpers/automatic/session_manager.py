"""
Session Manager for Voice Agents

Primary Objective:
This module serves as the central tracking and cleanup authority for all voice agent
subprocesses. Its main purpose is to ensure that no process is ever orphaned and that
the application's state remains consistent, regardless of how a process is created
(from the pool or directly) or how it terminates (normally, with an error, or during
application shutdown).

How it Works:
It uses a simple dictionary (`bot_procs`) as a registry for all running subprocesses,
keyed by their Process ID (PID). It provides functions to manage this registry and,
most importantly, runs a continuous background monitoring task to clean up any
processes that have terminated unexpectedly.

Key Features:
- A central, global dictionary (`bot_procs`) for tracking all live subprocesses.
- A background "janitor" task (`monitor_session_cleanup`) that periodically checks
  for and cleans up dead processes.
- A robust shutdown handler (`cleanup_bot_processes`) to terminate all running
  processes when the application exits.
- Callback functions to allow the process pool to trigger session cleanup without
  creating circular dependencies.
"""

import asyncio
import subprocess  # for TimeoutExpired
from typing import Any, Dict

from app.core.logger import logger

# The global registry for all running voice agent subprocesses.
# It maps the process ID (PID) to a tuple containing the process object and its metadata.
# Structure: {pid: (process_object, room_url, session_id, process_type)}
# process_type can be "pool" or "direct".
bot_procs: Dict[int, Any] = {}


async def cleanup_session_tracking(pid: int):
    """
    Removes a process from the `bot_procs` tracking dictionary.

    This is the definitive action for cleaning up a session's tracking entry. It is
    called after a process has terminated or been returned to the pool.
    """
    if pid in bot_procs:
        proc_info = bot_procs.pop(pid, None)
        if proc_info and len(proc_info) >= 4:
            _, _, session_id, _ = proc_info[:4]
            logger.info(
                f"Cleaned up session tracking for PID {pid} (session: {session_id})"
            )
        else:
            # Fallback for older or incomplete tracking entries.
            logger.info(f"Cleaned up legacy session tracking for PID {pid}")


async def session_cleanup_callback(session_id: str):
    """
    Finds a process by its `session_id` and triggers its tracking cleanup.

    Purpose:
    This function is passed as a callback to the VoiceAgentPool. It allows the pool
    to request the cleanup of a session's tracking entry without needing direct
    access to the `bot_procs` dictionary, thus avoiding circular imports.

    Flow:
    1. Iterates through the `bot_procs` dictionary.
    2. Finds the entry where the session ID matches.
    3. Calls `cleanup_session_tracking` with the corresponding PID.
    """
    pid_to_remove = None
    for pid, proc_info in list(bot_procs.items()):
        if len(proc_info) >= 3 and proc_info[2] == session_id:
            pid_to_remove = pid
            break
    if pid_to_remove:
        await cleanup_session_tracking(pid_to_remove)


async def monitor_session_cleanup():
    """
    A background task that acts as a safety net for cleaning up terminated processes.

    Purpose:
    This function is the application's "janitor." It handles cases where a subprocess
    might crash or exit without its normal cleanup logic being called. It ensures
    that no stale entries are left in the `bot_procs` dictionary, preventing memory leaks.

    Flow:
    1. Runs in an infinite `while True` loop.
    2. Iterates through all tracked processes in `bot_procs`.
    3. For each process, it calls `proc.poll()` or checks `proc.returncode`. If the
       result is not `None`, the process has terminated.
    4. It collects the PIDs of all terminated processes.
    5. It calls `cleanup_session_tracking` for each terminated process.
    6. It sleeps for 10 seconds, consuming no CPU, before repeating the check.
    """
    while True:
        try:
            pids_to_remove = []
            # Iterate over a snapshot of the items to prevent RuntimeError
            for pid, proc_info in list(bot_procs.items()):
                if len(proc_info) >= 4:
                    proc, _, _, _ = proc_info
                    # `poll()` and `returncode` are non-blocking ways to check process status.
                    if hasattr(proc, "poll") and proc.poll() is not None:
                        pids_to_remove.append(pid)
                    elif hasattr(proc, "returncode") and proc.returncode is not None:
                        pids_to_remove.append(pid)

            for pid in pids_to_remove:
                await cleanup_session_tracking(pid)

        except asyncio.CancelledError:
            logger.info("Session cleanup monitor task cancelled.")
            raise  # Re-raise the cancellation error to allow graceful shutdown
        except Exception as e:
            logger.error(f"Error in session cleanup monitor: {e}", exc_info=True)

        await asyncio.sleep(10)


async def cleanup_bot_processes():
    """
    Terminates all tracked bot processes during application shutdown.

    Purpose:
    This function is called from the `lifespan` manager in `app/main.py` when the
    application is shutting down. It ensures that no voice agent subprocesses are
    left running as orphaned processes.

    Flow:
    1. Iterates through a copy of the `bot_procs` dictionary.
    2. For each process, it checks if it is still running.
    3. If it is, it sends a `terminate()` signal (SIGTERM), which is a graceful
       shutdown request.
    4. It then waits for the process to exit.
    5. Finally, it removes the entry from the dictionary.
    """
    logger.info(f"Attempting to terminate {len(bot_procs)} bot processes.")
    for pid, proc_info in list(bot_procs.items()):
        try:
            if len(proc_info) == 4:
                proc, room_url, session_id, _ = proc_info
            else:
                proc, room_url = proc_info[:2]
                session_id = "unknown"

            # Check if the process is still alive.
            if isinstance(proc, asyncio.subprocess.Process):
                is_running = proc.returncode is None
            else:
                is_running = proc.poll() is None  # For standard subprocess.Popen

            if is_running:
                logger.info(
                    f"Terminating process {pid} for room {room_url} (session: {session_id})..."
                )
                proc.terminate()
                try:
                    # Wait for a few seconds for graceful shutdown
                    if isinstance(proc, asyncio.subprocess.Process):
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    else:
                        await asyncio.to_thread(proc.wait, timeout=5.0)
                    logger.info(f"Process {pid} terminated gracefully.")
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    logger.warning(
                        f"Process {pid} did not terminate gracefully. Escalating to kill."
                    )
                    try:
                        proc.kill()
                        if isinstance(proc, asyncio.subprocess.Process):
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
                        else:
                            await asyncio.to_thread(proc.wait, timeout=2.0)
                        logger.info(f"Process {pid} killed successfully.")
                    except Exception as kill_e:
                        logger.error(f"Error killing process {pid}: {kill_e}")
            else:
                logger.info(
                    f"Process {pid} for room {room_url} has already terminated."
                )
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}", exc_info=True)
        finally:
            # Ensure the process is removed from tracking even if termination fails.
            bot_procs.pop(pid, None)
    logger.info("All bot processes have been handled.")
