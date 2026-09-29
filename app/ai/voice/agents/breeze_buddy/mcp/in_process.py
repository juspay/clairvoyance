"""Answers tool-server URLs that name our own MCP endpoint, in this process.

A template's tool server names ``/mcp/<platform>/<store host>``.
``transport`` answers that URL here, with no network call. The public route
(``app/api/routers/mcp.py``) uses ``tool_call`` and is off unless
``MCP_PUBLIC_ENDPOINT_ENABLED``.
"""

from __future__ import annotations

import functools
import json
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.ai.voice.agents.breeze_buddy.mcp import server
from app.ai.voice.agents.breeze_buddy.mcp.server import ToolCall

_PATH_RE = re.compile(r"/mcp/(?P<platform>[a-z0-9-]+)/(?P<store>[^/]+)/?")
# A store host as it may appear in the path: lowercase DNS labels.
_HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
)


def tool_call(platform: str, store: str) -> Optional[ToolCall]:
    """``call(tool_name, arguments)`` for ``store`` on a hosted platform, or
    None for a platform we do not host or a store that is not a host name.
    ``store`` must already be lowercase."""
    if platform != "woocommerce" or not _HOST_RE.fullmatch(store):
        return None
    # Imported here, not at the top: a top-level import is circular, and it
    # would load assist code in a process with no WooCommerce template.
    from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import tools

    return functools.partial(tools.call_tool, tools.store_api_url(store))


class _InProcessTransport(httpx.AsyncBaseTransport):
    """Answers the HTTP handler's JSON-RPC POST with ``server.handle`` in this
    process, so the request never leaves the pod."""

    def __init__(self, call: ToolCall) -> None:
        self._call = call

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The HTTP handler always sends a JSON body and its own timeout.
        body = json.loads(await request.aread())
        deadline_s = request.extensions["timeout"]["read"]
        answer = await server.handle(body, self._call, deadline_s=deadline_s)
        return httpx.Response(200, json=answer)


def transport(url: str) -> Optional[httpx.AsyncBaseTransport]:
    """The in-process transport for a tool-server URL as the template wrote
    it, or None when the URL is not one of ours. A ``{placeholder}`` store
    fails the host check and is never answered in process."""
    match = _PATH_RE.fullmatch(urlparse(url).path)
    call = tool_call(match["platform"], match["store"].lower()) if match else None
    return _InProcessTransport(call) if call is not None else None
