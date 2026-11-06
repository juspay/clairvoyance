"""SSRF-safe egress for MCP servers.

Every MCP request, redirect hops included, is checked at send time and connects
to a checked address, so neither a redirect nor DNS rebinding reaches an
internal target.
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx
from mcp.client.session_group import StreamableHttpParameters

from app.core.logger import logger
from app.core.security.ssrf import (
    _ADDRESS_CONNECT_CAP,
    EgressResolutionError,
    SSRFError,
    is_same_origin,
    redact_url,
    validate_egress_url,
)


async def _check_mcp_egress(url: str) -> List[str]:
    """Resolve the MCP server now and refuse internal targets; returns the
    checked IPs. The reason is logged, not raised: it reaches the LLM."""
    try:
        return await validate_egress_url(url)
    except EgressResolutionError:
        raise
    except SSRFError as e:
        logger.warning(f"[BUDDY_MCP] egress refused for {redact_url(url)}: {e}")
        raise SSRFError("MCP server blocked by egress policy") from None


class McpEgressTransport(httpx.AsyncBaseTransport):
    """Checks each request and connects to a checked address, not the name.

    One transport serves one MCP server: a hop to another host is refused, so
    the server's credentials and the tool arguments never leave it. The mcp
    library hangs on a raised error, so failures come back as a status instead.
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._origin: Optional[str] = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self._origin is None:
            self._origin = url
        elif not is_same_origin(self._origin, url):
            logger.warning(
                f"[BUDDY_MCP] refused redirect {redact_url(self._origin)} -> "
                f"{redact_url(url)}"
            )
            return httpx.Response(404, request=request)
        try:
            budget = (request.extensions.get("timeout") or {}).get("connect")
            ips = await asyncio.wait_for(_check_mcp_egress(url), budget)
        except (asyncio.TimeoutError, EgressResolutionError) as e:
            logger.warning(f"[BUDDY_MCP] DNS failed for {redact_url(url)}: {e!r}")
            return httpx.Response(504, request=request)
        except SSRFError:
            return httpx.Response(404, request=request)  # reason logged by the check

        # raw_host is the ASCII (punycode) name the certificate carries.
        tls_name = request.url.raw_host.decode("ascii")

        def pinned(ip: str, last: bool) -> httpx.Request:
            # A copy: httpx joins relative redirects onto the caller's request.
            extensions: Dict[str, Any] = {
                **request.extensions,
                "sni_hostname": tls_name,
            }
            if not last:  # a dead address must not eat the whole budget
                timeout: Dict[str, Any] = dict(request.extensions.get("timeout") or {})
                connect = timeout.get("connect") or _ADDRESS_CONNECT_CAP
                timeout["connect"] = min(connect, _ADDRESS_CONNECT_CAP)
                extensions["timeout"] = timeout
            return httpx.Request(
                request.method,
                request.url.copy_with(host=ip),
                headers=request.headers,
                stream=request.stream,
                extensions=extensions,
            )

        for ip in ips[:-1]:
            try:
                return await self._inner.handle_async_request(pinned(ip, False))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue  # never reached it, so the next address is safe
        return await self._inner.handle_async_request(pinned(ips[-1], True))

    async def aclose(self) -> None:
        await self._inner.aclose()


def _egress_safe_client(
    headers: Optional[dict] = None,
    timeout: Optional[httpx.Timeout] = None,
    auth: Optional[httpx.Auth] = None,
) -> httpx.AsyncClient:
    """Same client the mcp library builds, sending through the egress check."""
    return httpx.AsyncClient(
        follow_redirects=True,
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0),
        auth=auth,
        transport=McpEgressTransport(),
    )


class McpServerParameters(StreamableHttpParameters):
    """pipecat forwards every field to the mcp client, so this one reaches it."""

    httpx_client_factory: Any = _egress_safe_client
