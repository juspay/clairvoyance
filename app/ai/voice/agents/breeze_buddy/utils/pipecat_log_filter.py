"""Loguru sink filter for known-harmless Pipecat transport log messages.

Pipecat's FastAPIWebsocketClient.disconnect() calls ws.close() during pipeline
teardown (triggered by EndFrame). When the remote client has already closed the
connection first, Starlette raises:

    RuntimeError: Cannot call "send" once a close message has been sent.

Pipecat catches this and logs it at ERROR level from its own loguru logger.
This is a transport-layer race that is unreachable from application code —
the EndFrame teardown path (our shutdown) is identical in both the happy path
and the client-disconnect path. The error has zero impact on call flow, data
integrity, or cleanup.

The application's core logger (app/core/logger/__init__.py) integrates a check
in filter_spam_logs() that drops this specific record from all sinks.

This module provides the install sentinel and the string constants used by that
integration.

Reference: pipecat v1.1.0 src/pipecat/transports/websocket/fastapi.py
    async def disconnect(self):
        ...
        try:
            await self._websocket.close()
        except Exception as e:
            logger.error(f"{self} exception while closing the websocket: {e}")

Usage:
    Call `install_pipecat_log_filter()` once at application startup,
    before the first pipeline runs:

        from app.ai.voice.agents.breeze_buddy.utils.pipecat_log_filter import (
            install_pipecat_log_filter,
        )
        install_pipecat_log_filter()
"""

from loguru import logger

# The exact substrings Pipecat logs in FastAPIWebsocketClient.disconnect()
# Source: pipecat v1.1.0 src/pipecat/transports/websocket/fastapi.py
PIPECAT_WS_CLOSE_MARKER = "exception while closing the websocket"
STARLETTE_WS_CLOSE_ERROR = 'Cannot call "send" once a close message has been sent'

# Module-level sentinel for idempotency
_FILTER_INSTALLED = False


def install_pipecat_log_filter() -> None:
    """Activate the Pipecat WS close error suppression filter.

    Safe to call multiple times — idempotent via module-level sentinel.

    The filter is integrated into the application's filter_spam_logs()
    in app/core/logger/__init__.py and suppresses the record from all
    configured log sinks.

    Call once at application startup before any pipeline runs.
    """
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return
    _FILTER_INSTALLED = True
    logger.debug(
        "[pipecat_log_filter] Pipecat WS close error suppression filter active"
    )
