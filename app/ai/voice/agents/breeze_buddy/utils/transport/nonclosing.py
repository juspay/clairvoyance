"""Proxy that shields a live FastAPI WebSocket from pipecat transport teardown.

During an agent-to-agent transfer the old pipeline's transports call
``websocket.close()`` as part of EndFrame teardown (ref-counted in
``FastAPIWebsocketClient.disconnect``, pipecat ``transports/websocket/fastapi.py``).
The Agent owns the real close instead: this proxy forwards every attribute to the
underlying websocket but turns ``close()`` into a no-op. The final close goes
through the RAW websocket (``close_websocket_safely(self.ws, ...)``), never through
the proxy.
"""

from typing import Any, Optional

from fastapi import WebSocket

from app.core.logger import logger


class NonClosingWebSocket:
    """Attribute-forwarding proxy whose ``close()`` is a no-op.

    Wraps FastAPI/Starlette's WebSocket API (not pipecat's), so it is inherently
    pipecat-version-proof. ``__getattr__`` forwards everything the pipecat client
    and serializers touch (``client_state``, ``application_state``, ``send_text``,
    ``send_bytes``, ``receive``, ``iter_text``, ``query_params``, ...).
    """

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def close(self, code: int = 1000, reason: Optional[str] = None) -> None:
        """Suppressed — the Agent closes the raw websocket once at true call end."""
        logger.debug(
            f"NonClosingWebSocket: suppressed close(code={code}) — "
            "Agent owns the connection lifecycle"
        )

    def __getattr__(self, name: str) -> Any:
        # Only triggered for attributes not found on this instance (i.e. everything
        # except ``_ws`` and ``close``). Forwards to the wrapped websocket.
        return getattr(self._ws, name)
