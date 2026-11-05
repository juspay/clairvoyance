"""
Helpers for keeping blocking work off the asyncio event loop.

Why this module exists
----------------------
Each API pod runs uvicorn with a single worker: one event loop, one thread,
at most one CPU core. Any synchronous call inside an ``async def`` freezes
*every* other in-flight call for its full duration — there is no other
thread to pick up the slack.

On 27 Jul 2026 a single such line (``start_call_recording``, ~166 ms of
blocking Plivo HTTP per call) capped a pod at ~6 calls/sec and produced a
P1 orphan-alert storm (27 Jul 2026).

The two remedies, and when to use which
---------------------------------------
``spawn_background_task(coro)``
    The result is not needed to answer the request. Starts the coroutine and
    returns immediately.

``await asyncio.to_thread(fn, *args)``
    The callable is synchronous and blocking (a sync SDK, ``requests``,
    ``time.sleep``). Use it directly — there is deliberately no wrapper here,
    because ``asyncio.to_thread`` is already the idiomatic, greppable name.

They compose: ``spawn_background_task(_do_it())`` where ``_do_it`` internally does
``await asyncio.to_thread(blocking_fn, ...)``. Both are required — the first
so nobody waits, the second so the loop is never occupied.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional, Set

from app.core.logger import logger

# Strong references to in-flight background tasks.
#
# asyncio.create_task only keeps a *weak* reference to the task it returns.
# If the caller drops its reference (which fire-and-forget by definition does),
# the task can be garbage-collected mid-await and silently cancelled. Holding
# it in a module-level set until the done-callback fires prevents that.
# https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_tasks: Set[asyncio.Task] = set()


def _log_background_failure(task: asyncio.Task) -> None:
    """Drain the task's exception so it is logged, not swallowed by the GC."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # ``logger`` is loguru, not stdlib logging: it has no ``exc_info``
        # kwarg. Any kwarg passed here is used for ``str.format`` on the
        # already-interpolated message, so passing exc_info=exc would raise
        # KeyError whenever str(exc) contains braces (e.g. a dict/JSON body)
        # and silently drop the traceback the rest of the time. Use
        # ``logger.opt(exception=...)`` instead, which is loguru's actual
        # mechanism for attaching a traceback to a log record.
        logger.opt(exception=exc).error(
            f"[background] task '{task.get_name()}' failed: {exc}"
        )


def spawn_background_task(
    coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None
) -> asyncio.Task:
    """
    Run ``coro`` without awaiting it, keeping a strong reference until it ends.

    Failures are logged and never propagate to the caller — a background task
    must not be able to break the request that spawned it.

    Note:
        Background tasks are NOT drained at application shutdown. A spawned
        coroutine must not assume the DB pool, Redis, or the logger are
        still available late in the process lifecycle — it may still be
        running (or start running) after shutdown has begun tearing those
        down.

    Args:
        coro: The coroutine to run. Create it inline: ``spawn_background_task(f())``.
        name: Optional task name, used in failure logs.

    Returns:
        The created ``asyncio.Task`` — useful in tests; production callers
        can ignore it.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_background_failure)
    return task
