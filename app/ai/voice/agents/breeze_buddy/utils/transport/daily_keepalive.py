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

from typing import Any, Callable

from app.core.logger import logger


def hold_daily_client(client: Any) -> Callable[[], None]:
    """Neutralise a ``DailyTransportClient``'s ``leave()``/``cleanup()``.

    Replaces both with no-ops so per-generation pipeline teardown cannot drop the
    Daily connection. Returns a ``restore()`` that puts the original methods back
    (not required for teardown — :func:`force_teardown_daily_client` uses the
    low-level ``_leave``/``_cleanup`` which are left untouched).
    """
    original_leave = client.leave
    original_cleanup = client.cleanup

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

    Bypasses the join/leave refcount (bumped high by the no-op ``leave()`` +
    per-generation joins) by calling the low-level ``_leave``/``_cleanup``
    directly. Best-effort: failures are logged, never raised, so call teardown is
    never blocked.
    """
    try:
        leave = getattr(client, "_leave", None)
        if leave is not None:
            await leave()
    except Exception as exc:  # noqa: BLE001 - teardown must never raise
        logger.warning(f"[daily_keepalive] force leave failed: {exc}")
    try:
        cleanup = getattr(client, "_cleanup", None)
        if cleanup is not None:
            cleanup()
    except Exception as exc:  # noqa: BLE001 - teardown must never raise
        logger.warning(f"[daily_keepalive] force cleanup failed: {exc}")
