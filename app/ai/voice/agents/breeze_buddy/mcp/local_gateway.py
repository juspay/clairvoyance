"""In-process tool gateways for ``local://<name>/<target>`` MCP server URLs.

For platforms with no remote UCP endpoint (Shopify has ``/api/ucp/mcp``).
The direct-HTTP handler posts through :func:`transport_for`, so both kinds of
server share one request and result path. ``<name>`` is a package under
``assist/platforms/`` that registers its gateway on import; the first call
imports it if nothing has yet. A gateway raising :class:`GatewayToolError`
returns an ``isError`` result.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import httpx

from app.core.logger import logger

LOCAL_SCHEME = "local"
_PLATFORMS_PACKAGE = "app.ai.voice.agents.breeze_buddy.assist.platforms"

LocalGatewayFn = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

_GATEWAYS: Dict[str, LocalGatewayFn] = {}
# Imported once per name; a failed import is not retried.
_IMPORTED: Set[str] = set()


class GatewayToolError(Exception):
    """A tool-level failure the model should read (``isError: true``)."""

    def __init__(self, payload: Dict[str, Any]):
        super().__init__(json.dumps(payload))
        self.payload = payload


def register_local_gateway(name: str, fn: LocalGatewayFn) -> None:
    """Serve ``local://<name>/…`` with ``fn`` (idempotent per name)."""
    _GATEWAYS[name] = fn


def _gateway(name: str) -> Optional[LocalGatewayFn]:
    if name not in _GATEWAYS and name.isidentifier() and name not in _IMPORTED:
        _IMPORTED.add(name)
        try:
            importlib.import_module(f"{_PLATFORMS_PACKAGE}.{name}")
        except ImportError as e:
            logger.warning(f"[BUDDY_MCP] no platform package for local://{name}: {e}")
    return _GATEWAYS.get(name)


class _LocalGatewayTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gateway = _gateway(request.url.host)
        if gateway is None:
            return httpx.Response(404, text=f"no local gateway {request.url.host!r}")
        body = json.loads(request.content)
        params = body.get("params") or {}
        # A custom transport gets httpx's timeout but must apply it itself.
        timeout = (request.extensions.get("timeout") or {}).get("read")
        try:
            payload = await asyncio.wait_for(
                gateway(
                    request.url.path.strip("/"),
                    params.get("name", ""),
                    params.get("arguments") or {},
                ),
                timeout,
            )
            is_error = False
        except GatewayToolError as e:
            payload, is_error = e.payload, True
        except asyncio.TimeoutError:
            return httpx.Response(504, text=f"local gateway timed out after {timeout}s")
        except Exception as e:
            logger.warning(f"[BUDDY_MCP] local gateway {request.url.host!r}: {e}")
            return httpx.Response(502, text=f"local gateway error: {e}")
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": is_error,
                },
            },
        )


def transport_for(url: str) -> Optional[httpx.AsyncBaseTransport]:
    """The in-process transport for a ``local://`` URL; ``None`` otherwise
    (httpx then uses its default network transport)."""
    if url.startswith(f"{LOCAL_SCHEME}://"):
        return _LocalGatewayTransport()
    return None
