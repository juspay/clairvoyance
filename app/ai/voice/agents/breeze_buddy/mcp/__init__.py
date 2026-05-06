"""Generic MCP integration for Breeze Buddy - supports any MCP server."""

import asyncio
import base64
import re
from datetime import timedelta
from typing import Any, Dict, List, cast

from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.llm_service import (
    FunctionCallParams,
    FunctionCallResultProperties,
)
from pipecat.services.mcp_service import MCPClient
from pipecat_flows.types import FlowResult, FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.mcp.cache import get_or_discover_server_tools
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthType,
    McpConfig,
    McpServerConfig,
)
from app.core.logger import logger


# NOTE: Uses private _tool_wrapper to integrate with FlowManager's global
# functions. Public register_tools(llm) bypasses FlowManager orchestration.
# Pin pipecat-ai version to guard against API changes.
def _create_mcp_tool_handler(
    server_params: StreamableHttpParameters,
    tool_name: str,
) -> Any:
    """Create a FlowManager-compatible handler that creates a fresh MCPClient per call.

    A new MCPClient is created for each invocation to avoid race conditions when
    the LLM makes parallel tool calls. The client is cleaned up after each call.
    """

    async def handler(args: Dict[str, Any], flow_manager: Any) -> FlowResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        async def result_callback(
            result: Any,
            *,
            properties: FunctionCallResultProperties | None = None,
        ) -> None:
            if not future.done():
                future.set_result(result)

        params = FunctionCallParams(
            function_name=tool_name,
            tool_call_id="",
            arguments=args,
            llm=None,  # type: ignore[arg-type]
            context=None,  # type: ignore[arg-type]
            result_callback=result_callback,
        )

        try:
            async with MCPClient(server_params=server_params) as mcp_client:
                await asyncio.wait_for(mcp_client._tool_wrapper(params), timeout=30)
            result = await asyncio.wait_for(future, timeout=30)
            return cast(FlowResult, {"status": "success", "data": result})
        except asyncio.TimeoutError:
            return cast(FlowResult, {"status": "error", "data": "MCP tool timeout"})
        except Exception as e:
            logger.warning(f"[BUDDY_MCP] Tool '{tool_name}' failed: {e}")
            return cast(FlowResult, {"status": "error", "data": f"MCP tool error: {e}"})

    return handler


def _resolve_placeholders(value: str, template_vars: Dict[str, Any]) -> str:
    """Substitute {variable} placeholders in a string using template_vars.

    Uses single-pass regex substitution to prevent cascading substitution
    (e.g., a value containing {other_key} won't be re-substituted).

    Only substitutes values that are strings — list/dict/None values in
    template_vars (e.g. the ``items`` payload field) are skipped.
    """

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        val = template_vars.get(key)
        return val if isinstance(val, str) else match.group(0)

    return re.sub(r"\{([^{}]+)\}", replacer, value)


def _build_auth_headers(
    server: McpServerConfig,
    template_vars: Dict[str, Any],
) -> Dict[str, str]:
    """Resolve auth config into HTTP headers, substituting {variable} placeholders."""
    if not server.auth or server.auth.type == HttpAuthType.NONE:
        return {}

    def resolve(value: str) -> str:
        return _resolve_placeholders(value, template_vars)

    auth = server.auth
    if auth.type == HttpAuthType.BEARER and auth.token:
        token = resolve(auth.token.get_secret_value())
        return {"Authorization": f"Bearer {token}"}

    if auth.type == HttpAuthType.API_KEY and auth.api_key_name and auth.api_key_value:
        key = resolve(auth.api_key_value.get_secret_value())
        return {auth.api_key_name: key}

    if auth.type == HttpAuthType.BASIC and auth.username and auth.password:
        creds = base64.b64encode(
            f"{auth.username}:{resolve(auth.password.get_secret_value())}".encode()
        ).decode()
        return {"Authorization": f"Basic {creds}"}

    return {}


def _build_server_params(
    server: McpServerConfig,
    template_vars: Dict[str, Any],
) -> StreamableHttpParameters:
    """Resolve a server config + template_vars into StreamableHttpParameters.

    Shared by the voice loader (per-call clients) and the chat session pool
    (per-turn persistent clients). Substitutes ``{variable}`` placeholders in
    the URL and auth fields from ``template_vars``.
    """
    resolved_url = _resolve_placeholders(server.url, template_vars)
    if resolved_url != server.url:
        # Don't log the resolved URL — it can contain customer-identifying
        # values (e.g. shop subdomain). Operators can correlate via the
        # stable label logged at the call site.
        logger.debug(
            f"[BUDDY_MCP] Resolved URL placeholder for server {server.name or '<unnamed>'!r}"
        )

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    headers.update(server.headers)  # static headers from config
    headers.update(_build_auth_headers(server, template_vars))  # auth headers

    return StreamableHttpParameters(
        url=resolved_url,
        headers=headers,
        timeout=timedelta(seconds=server.timeout),
        sse_read_timeout=timedelta(seconds=server.timeout),
        terminate_on_close=True,
    )


async def _load_server_tools(
    server: McpServerConfig,
    template_vars: Dict[str, str],
    existing_names: set,
) -> List[FlowsFunctionSchema]:
    """Connect to a single MCP server and return its tools as FlowsFunctionSchema.

    The server URL supports {variable} placeholder substitution from template_vars.
    For example, a URL of ``https://{shop_url}/ai/mcp`` will have ``{shop_url}``
    replaced with the value of ``shop_url`` from template_vars (passed in from the
    Nautilus call payload via the lead's ``shop_url`` field).

    Auth credentials (api_key_value, bearer token, etc.) are also resolved from
    template_vars, which includes values from the credentials table.
    For servers that need no authentication, set ``auth.type`` to ``none`` or
    leave ``auth`` as null.

    Each tool handler creates a fresh MCPClient per invocation for thread safety.
    """
    server_params = _build_server_params(server, template_vars)
    # Prefer the stable name; fall back to the raw template URL (with
    # placeholders) rather than the resolved URL to avoid logging
    # customer-identifying substitutions.
    label = server.name or server.url
    logger.info(f"[BUDDY_MCP] Connecting to {label}")

    # Use a temporary client just to fetch the tools schema
    async with MCPClient(server_params=server_params) as temp_client:
        tools_schema = await asyncio.wait_for(
            temp_client.get_tools_schema(),
            timeout=server.timeout,
        )

    functions: List[FlowsFunctionSchema] = []
    for func_schema in tools_schema.standard_tools:
        # Prefix tool name with server name only on collision
        tool_name = func_schema.name
        if tool_name in existing_names and server.name:
            tool_name = f"{server.name}_{tool_name}"

        functions.append(
            FlowsFunctionSchema(
                name=tool_name,
                description=func_schema.description,
                properties=func_schema.properties,
                required=func_schema.required,
                handler=_create_mcp_tool_handler(server_params, func_schema.name),
            )
        )
        # Track the chosen name so a subsequent tool from the same server
        # with a duplicate name also gets prefixed.
        existing_names.add(tool_name)
        logger.info(f"[BUDDY_MCP] Registered tool: {tool_name} (from {label})")

    logger.info(f"[BUDDY_MCP] Loaded {len(functions)} tools from {label}")
    return functions


async def get_mcp_global_functions(
    mcp_config: McpConfig,
    template_vars: Dict[str, str] | None = None,
) -> List[FlowsFunctionSchema]:
    """Fetch tools from all enabled MCP servers and return as FlowManager global functions.

    Each tool handler creates a fresh MCPClient per invocation, so no shared
    client cleanup is needed at the call level.
    """
    template_vars = template_vars or {}
    all_functions: List[FlowsFunctionSchema] = []

    for server in mcp_config.servers:
        if not server.enabled:
            continue
        label = server.name or server.url
        try:
            existing_names = {f.name for f in all_functions}
            task = asyncio.create_task(
                _load_server_tools(server, template_vars, existing_names)
            )
            tools = await asyncio.shield(task)
            all_functions.extend(tools)
        except Exception as e:
            logger.error(
                f"[BUDDY_MCP] Failed to load tools from {label}, skipping: {type(e).__name__}: {e}"
            )

    logger.info(f"[BUDDY_MCP] Total tools loaded: {len(all_functions)}")
    return all_functions


async def get_mcp_global_functions_cached(
    mcp_config: McpConfig,
    template_vars: Dict[str, Any],
    template_id: str,
) -> List[FlowsFunctionSchema]:
    """Cache-aware variant for chat mode.

    Reads tool metadata from Redis (per-template + URL hash, see
    ``mcp/cache.py``) and rebuilds ``FlowsFunctionSchema`` using the same
    ``_create_mcp_tool_handler`` voice uses (per-invocation MCPClient — no
    private API, no shared session). Auth headers are rebuilt from
    ``template_vars`` on every call so credential rotation takes effect on
    the next turn without cache invalidation.
    """
    all_functions: List[FlowsFunctionSchema] = []

    for server in mcp_config.servers:
        if not server.enabled:
            continue

        try:
            server_params = _build_server_params(server, template_vars)
        except Exception as e:
            logger.error(
                f"[BUDDY_MCP] chat: failed to build server params for "
                f"{server.name or server.url}: {type(e).__name__}: {e}"
            )
            continue

        # Stable label: prefer server.name; fall back to the raw template
        # URL (placeholder form) rather than the resolved URL.
        label = server.name or server.url
        try:
            tools_meta = await get_or_discover_server_tools(
                template_id=template_id,
                server=server,
                server_params=server_params,
            )
        except Exception as e:
            logger.error(
                f"[BUDDY_MCP] chat: failed to load tools from {label}, "
                f"skipping: {type(e).__name__}: {e}"
            )
            continue

        existing_names = {f.name for f in all_functions}
        for meta in tools_meta:
            tool_name = meta["name"]
            if tool_name in existing_names and server.name:
                tool_name = f"{server.name}_{tool_name}"
            all_functions.append(
                FlowsFunctionSchema(
                    name=tool_name,
                    description=meta["description"],
                    properties=meta["properties"],
                    required=meta["required"],
                    handler=_create_mcp_tool_handler(server_params, meta["name"]),
                )
            )
            existing_names.add(tool_name)
            logger.debug(
                f"[BUDDY_MCP] chat: registered tool: {tool_name} (from {label})"
            )

    logger.info(f"[BUDDY_MCP] chat: total tools loaded: {len(all_functions)}")
    return all_functions
