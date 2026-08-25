"""WebSocket utilities for telephony voice agents."""

import json

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.core.logger import logger

# Starlette/uvicorn raise these when the ASGI socket is already torn down —
# the caller hung up and the disconnect event was already consumed by a
# cancelled receive/send. This is a teardown race, not a code bug: the alerts
# digest logged ~790 of these as errors across 3 days with zero live-call
# impact (the caller is already gone). is_caller_disconnected_error
# classifies them so they surface as warnings; anything else stays an error.
_TEARDOWN_ERROR_MARKERS = (
    # receive() after the disconnect message was already delivered (starlette)
    'Cannot call "receive" once a disconnect message has been received.',
    # send_text after the app already emitted a close (starlette)
    'Cannot call "send" once a close message has been sent.',
    # used before accept — instant-disconnect callers (starlette)
    'WebSocket is not connected. Need to call "accept" first.',
)

# uvicorn's "Unexpected ASGI message '<msg>', after sending 'websocket.close'"
# family has been reworded across versions ("after sending 'x'" in 0.34.x,
# "after 'x'" in the wording seen in the Aug 2026 alert catalog). Match the
# family prefix, scoped to websocket.* messages so the HTTP variants
# ("response already completed") never classify as teardown.
_ASGI_UNEXPECTED_PREFIX = "Unexpected ASGI message"
_ASGI_WEBSOCKET_SCOPE = "websocket."

# Close codes that mean the REMOTE side ended the call normally — the
# caller-hangup case. Abnormal codes (1006 connection drop, 1011 server
# error, 1013 protocol violation…) are infra/protocol failures and must stay
# ERROR: an LB idle timeout killing menus is not "caller disconnected".
_CALLER_CLOSE_CODES = frozenset({1000, 1001, 1005})


def is_caller_disconnected_error(error: BaseException) -> bool:
    """True when the error is the expected teardown race after the caller hung up.

    Args:
        error: Exception raised by a websocket receive/send/close call.

    Returns:
        True if the error means the remote side is already gone (log as
        warning), False for anything unexpected (log as error). Never raises
        — callers invoke this inside except blocks, where a second exception
        would break their error contracts.
    """
    try:
        if isinstance(error, WebSocketDisconnect):
            return error.code in _CALLER_CLOSE_CODES
        message = str(error)
        if any(marker in message for marker in _TEARDOWN_ERROR_MARKERS):
            return True
        return _ASGI_UNEXPECTED_PREFIX in message and _ASGI_WEBSOCKET_SCOPE in message
    except Exception:
        # Un-classifiable (e.g. an exception whose __str__ raises) — treat as
        # unexpected so it logs at ERROR rather than being swallowed.
        return False


def is_disconnected(ws: WebSocket) -> bool:
    """True when the websocket state already reports DISCONNECTED.

    Never raises — returns False when the state can't be read (odd/duck-typed
    socket), so callers fall through to the send attempt.
    """
    try:
        return ws.client_state is WebSocketState.DISCONNECTED
    except Exception:
        return False


async def close_websocket_safely(
    ws: WebSocket,
    code: int = 1000,
    reason: str = "",
) -> None:
    """Safely close websocket connection.

    Args:
        ws: WebSocket connection
        code: Close code (default 1000 for normal closure)
        reason: Close reason message
    """
    try:
        if not is_disconnected(ws):
            await ws.close(code=code, reason=reason)
    except Exception as e:
        logger.warning(f"Could not close websocket (likely already closed): {e}")


async def send_message(
    ws: WebSocket,
    message: dict,
) -> bool:
    """Send a message over websocket.

    Args:
        ws: WebSocket connection
        message: Message dictionary to send

    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        if is_disconnected(ws):
            # Caller already gone and the stack knows it — nothing to deliver.
            logger.debug("Skipped websocket send: caller already disconnected")
            return False
        await ws.send_text(json.dumps(message))
        logger.debug("Successfully sent websocket message")
        return True
    except Exception as e:
        # A WebSocketDisconnect raised by a SEND means the remote side is
        # gone — whatever the code. Real-uvicorn e2e showed a CLEAN client
        # close racing our send surfaces as code 1006 here (no close frame
        # processed before our send), so the receive-path close-code
        # allowlist must not gate the send path: there is no caller to
        # deliver to either way. The allowlist still guards the receive-side
        # catch-alls (IVR menu, v2 handler), where 1006 means infra drop.
        if (
            isinstance(e, WebSocketDisconnect)
            or is_caller_disconnected_error(e)
            or is_disconnected(ws)
        ):
            # Teardown race: the send raced the caller's disconnect. Expected
            # on every hang-up that lands mid-audio; warn, don't error.
            logger.warning(f"Websocket send failed (caller disconnected): {e}")
        else:
            logger.error(f"Failed to send websocket message: {e}")
        return False
