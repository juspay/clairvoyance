"""WebSocket utilities for telephony voice agents."""

import json

from fastapi import WebSocket

from app.core.logger import logger


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
        if ws.client_state.name != "DISCONNECTED":
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
        await ws.send_text(json.dumps(message))
        logger.debug("Successfully sent websocket message")
        return True
    except Exception as e:
        logger.error(f"Failed to send websocket message: {e}")
        return False
