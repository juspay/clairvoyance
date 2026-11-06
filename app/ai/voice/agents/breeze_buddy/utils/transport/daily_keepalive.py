"""Daily transport keep-alive for agent-to-agent transfer.

The Daily analog of ``NonClosingWebSocket``.

During a transfer the outgoing pipeline's EndFrame teardown calls, on the Daily
input+output transports, ``DailyTransportClient.leave()`` (which — once the
join/leave refcount hits 0 — actually leaves the room) and ``cleanup()`` (which
``release()``s the underlying daily-python ``CallClient``). Either one empties
the room, ends the Daily meeting session, and **ejects the browser client**.

Telephony avoids this because its connection is an external ``ws`` we pass into
``_create_telephony_transport`` and ``NonClosingWebSocket`` no-ops its ``close()``.
Daily's connection (the ``DailyTransportClient``) is created *inside* the
transport, so the equivalent trick is to neutralise the client's ``leave()`` and
``cleanup()`` for the duration of the call. The **same** joined client is then
reused across pipeline generations (see ``agent/transfer.py::apply_transfer``),
so the room/meeting-session — and the browser client — stay put. The Agent owns
the one real teardown at true call end via :func:`force_teardown_daily_client`
(mirroring telephony's final ``close_websocket_safely``).
"""

import asyncio
from typing import Any, Callable

from app.core.logger import logger

# Real teardown deadlines. Generous — a healthy leave/release finishes in
# well under a second; the timeout only exists so a pathological native
# hang strands one executor thread instead of the caller.
_LEAVE_TIMEOUT_SECS = 10
_CLEANUP_TIMEOUT_SECS = 15


def hold_daily_client(client: Any) -> Callable[[], None]:
    """Neutralise a ``DailyTransportClient``'s ``leave()``/``cleanup()``.

    Replaces both with no-ops so per-generation pipeline teardown cannot drop the
    Daily connection. The originals are stashed on the client so
    :func:`force_teardown_daily_client` can run the REAL async ``cleanup()`` at
    true call end — that method (pipecat's) is the only path that cancels the
    event/audio/video callback tasks and releases the native ``CallClient`` off
    the event loop. Returns a ``restore()`` that puts the original methods back.
    """
    original_leave = client.leave
    original_cleanup = client.cleanup

    # Stash for force_teardown_daily_client. Attribute names are
    # namespaced to avoid colliding with pipecat internals.
    client._bb_held_leave = original_leave
    client._bb_held_cleanup = original_cleanup

    async def _suppressed_leave(*args: Any, **kwargs: Any) -> None:
        logger.debug("[daily_keepalive] suppressed DailyTransportClient.leave()")

    async def _suppressed_cleanup(*args: Any, **kwargs: Any) -> None:
        logger.debug("[daily_keepalive] suppressed DailyTransportClient.cleanup()")

    client.leave = _suppressed_leave
    client.cleanup = _suppressed_cleanup

    def restore() -> None:
        client.leave = original_leave
        client.cleanup = original_cleanup

    return restore


async def force_teardown_daily_client(client: Any) -> None:
    """Really leave the room and release the CallClient at true call end.

    Best-effort with hard deadlines: failures are logged, never raised, so
    call teardown is never blocked.

    Two hard-won constraints shape this function:

    1. ``CallClient.release()`` is a BLOCKING native call that waits for the
       client's worker threads to finish — and those threads may need the
       GIL / event loop to deliver their final callbacks. Calling it on the
       event-loop thread therefore deadlocks the entire server (every HTTP
       request hangs; observed in local dev after each widget voice call).
       It must only ever run off-loop.

    2. pipecat's async ``DailyTransportClient.cleanup()`` is the one method
       that BOTH cancels the event/audio/video callback tasks (otherwise
       reported as dangling by PipelineTask) AND runs ``release()`` in an
       executor. Since :func:`hold_daily_client` no-ops ``cleanup`` on the
       instance, we call the stashed original here.
    """
    # Leave the room via the low-level _leave (bypasses the join/leave
    # refcount, which the no-op leave() left inflated).
    try:
        leave = getattr(client, "_leave", None)
        if leave is not None:
            await asyncio.wait_for(leave(), timeout=_LEAVE_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        logger.warning(
            f"[daily_keepalive] force leave timed out after {_LEAVE_TIMEOUT_SECS}s; "
            "continuing to cleanup"
        )
    except Exception as exc:  # noqa: BLE001 - teardown must never raise
        logger.warning(f"[daily_keepalive] force leave failed: {exc}")

    # Real cleanup: cancel callback tasks + release the native client
    # off-loop, via the original (pre-hold) async cleanup().
    try:
        cleanup = getattr(client, "_bb_held_cleanup", None)
        if cleanup is not None:
            await asyncio.wait_for(cleanup(), timeout=_CLEANUP_TIMEOUT_SECS)
        else:
            # Client was never held (shouldn't happen) — fall back to the
            # raw release, still strictly off-loop.
            raw_cleanup = getattr(client, "_cleanup", None)
            if raw_cleanup is not None:
                await asyncio.wait_for(
                    asyncio.to_thread(raw_cleanup), timeout=_CLEANUP_TIMEOUT_SECS
                )
    except asyncio.TimeoutError:
        # The native client is stranded (leaked worker threads), but the
        # event loop stays healthy and the next voice session gets a fresh
        # CallClient.
        logger.warning(
            f"[daily_keepalive] client cleanup timed out after "
            f"{_CLEANUP_TIMEOUT_SECS}s; native client leaked but loop unblocked"
        )
    except Exception as exc:  # noqa: BLE001 - teardown must never raise
        logger.warning(f"[daily_keepalive] force cleanup failed: {exc}")
