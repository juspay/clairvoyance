"""Redis cache for MCP tool discovery.

MCP ``list_tools`` is an HTTP round-trip to the configured server. Without
caching, every chat ``/message`` would pay this cost per server. The cache
keys per ``(template_id, server-identity-hash)`` so all sessions of the
same template + same resolved URL share one discovery for the TTL window.

What's cached: only the public tool metadata (name, description, input
schema). Auth credentials are NOT in the key or in the value — they're
rebuilt per turn from ``template_vars`` (loaded fresh from the credentials
table). Credential rotation therefore takes effect on the next turn with
no cache invalidation.

Best-effort: Redis errors fall through to live discovery so cache outages
don't break MCP tool availability.
"""

import asyncio
import hashlib
import json
from typing import Any, Dict, List, TypedDict

from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig
from app.core.logger import logger
from app.services.redis.client import get_redis_service, is_redis_configured

CACHE_TTL_SECONDS = 300
_KEY_PREFIX = "bb:mcp:tools:"


class ToolMeta(TypedDict):
    """Cached tool metadata. Mirrors FunctionSchema's public fields."""

    name: str
    description: str
    properties: Dict[str, Any]
    required: List[str]


def _cache_key(
    template_id: str,
    server: McpServerConfig,
    server_params: StreamableHttpParameters,
) -> str:
    """Hash the inputs that determine *which tools the server returns*.

    Auth is intentionally excluded — different credentials against the same
    server return the same tools schema, so they should share a cache entry.
    """
    label = server.name or ""
    headers_json = json.dumps(server.headers, sort_keys=True)
    raw = f"{label}|{server_params.url}|{server.timeout}|{headers_json}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{_KEY_PREFIX}{template_id}:{digest}"


_REQUIRED_TOOL_FIELDS = ("name", "description", "properties", "required")


def _is_valid_tools_payload(decoded: Any) -> bool:
    """Reject malformed cache entries before they reach the call site.

    Downstream callers index ``meta["name"]`` etc. unconditionally, so a
    half-formed entry would raise mid-iteration and skip the whole server.
    Validate shape here and force a re-discover instead.
    """
    if not isinstance(decoded, list):
        return False
    return all(
        isinstance(item, dict) and all(field in item for field in _REQUIRED_TOOL_FIELDS)
        for item in decoded
    )


async def _live_discovery(
    server_params: StreamableHttpParameters, timeout: int
) -> List[ToolMeta]:
    """Open a fresh MCPClient, fetch tools schema, return JSON-friendly metadata."""
    async with MCPClient(server_params=server_params) as client:
        tools_schema = await asyncio.wait_for(
            client.get_tools_schema(),
            timeout=timeout,
        )
    return [
        ToolMeta(
            name=fn.name,
            description=fn.description,
            properties=fn.properties,
            required=list(fn.required),
        )
        for fn in tools_schema.standard_tools
    ]


async def get_or_discover_server_tools(
    template_id: str,
    server: McpServerConfig,
    server_params: StreamableHttpParameters,
) -> List[ToolMeta]:
    """Return cached tools metadata, or discover via MCP and populate cache.

    Falls through to live discovery if Redis is unconfigured or
    misbehaving. Discovery failures propagate so the caller can decide
    whether to skip the server.
    """
    if not is_redis_configured():
        return await _live_discovery(server_params, server.timeout)

    key = _cache_key(template_id, server, server_params)
    redis = await get_redis_service()

    try:
        cached = await redis.get(key)
    except Exception as exc:
        logger.warning(f"[BUDDY_MCP] cache GET failed for {key}: {exc}")
        cached = None

    if cached is not None:
        try:
            decoded = json.loads(cached)
            if _is_valid_tools_payload(decoded):
                logger.debug(f"[BUDDY_MCP] cache hit: {key} ({len(decoded)} tool(s))")
                return decoded
            logger.warning(
                f"[BUDDY_MCP] cache payload for {key} failed shape check, refetching"
            )
        except Exception as exc:
            logger.warning(
                f"[BUDDY_MCP] cache decode failed for {key}, refetching: {exc}"
            )

    tools = await _live_discovery(server_params, server.timeout)

    try:
        await redis.setex(key, json.dumps(tools), ttl_seconds=CACHE_TTL_SECONDS)
        logger.info(f"[BUDDY_MCP] cache populated: {key} ({len(tools)} tool(s))")
    except Exception as exc:
        logger.warning(f"[BUDDY_MCP] cache SET failed for {key}: {exc}")

    return tools


__all__ = ["CACHE_TTL_SECONDS", "ToolMeta", "get_or_discover_server_tools"]
