"""Generic MCP integration for Breeze Buddy - supports any MCP server."""

import asyncio
import base64
import json
import re
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple, Union, cast

import httpx
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.llm_service import (
    FunctionCallParams,
    FunctionCallResultProperties,
)
from pipecat.services.mcp_service import MCPClient
from pipecat_flows.types import FlowResult, FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.tool_pipeline import (
    apply_result_pipeline_json_str,
)
from app.ai.voice.agents.breeze_buddy.mcp.cache import get_or_discover_server_tools

# gate_call wraps gated MCP tool handlers with the HITL approval gate (voice).
# Cycle-safe: template.approval only depends on template.context +
# template.types, never on mcp. (isort orders this template import among the
# others; the latent handlers<->template cycle the note below describes is
# pre-existing and unchanged by this import.)
from app.ai.voice.agents.breeze_buddy.template.approval import gate_call
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _inject_voice_state,
    _reduce_voice_state,
)

# Template types FIRST — fully loads the `template` package (whose
# __init__ eagerly pulls in http_handler / http_requester / hooks /
# field_resolver) before we touch handlers/transport/utils/. Importing
# the transform helper before this would trigger
# handlers.transport.utils.__init__ → field_resolver → template.types
# while template/__init__.py is mid-load, causing a cycle.
from app.ai.voice.agents.breeze_buddy.template.types import (
    ApprovalConfig,
    HttpAuthType,
    McpConfig,
    McpServerConfig,
    ResponseTransform,
    ToolUiHint,
)
from app.core.logger import logger
from app.core.security.ssrf import validate_egress_url

# --- HITL approval for MCP tools -------------------------------------------
# Watchdog budget for a gated MCP tool: approval wait + the handler's dispatch
# ceiling + margin. The MCP handler does up to two 30s waits (_tool_wrapper +
# the result future), so the exec ceiling is ~60s. Mirrors the ADDITIVE budget
# in template/global_function.py — pipecat's watchdog fires
# result_callback(None) rather than cancelling the handler, so an undersized
# timeout would silently drop the real result while the side effect still runs.
_MCP_APPROVAL_EXEC_BUDGET_SECS = 60.0
_MCP_APPROVAL_BUDGET_MARGIN_SECS = 15.0


def _mcp_approval_timeout_secs(approval: ApprovalConfig) -> float:
    """Additive pipecat watchdog budget for a gated MCP FlowsFunctionSchema."""
    return (
        approval.timeout_secs
        + _MCP_APPROVAL_EXEC_BUDGET_SECS
        + _MCP_APPROVAL_BUDGET_MARGIN_SECS
    )


def _gate_mcp_handler(
    handler: Any,
    bot_instance: Any,
    registered_name: str,
    approval: Optional[ApprovalConfig],
) -> Any:
    """Wrap an MCP tool handler with the HITL approval gate (voice path).

    No-op when there is no approval config or no bot to reach the
    ApprovalManager — returns the handler unchanged. On chat the bot sets
    ``handles_approval_externally`` so ``gate_call`` short-circuits to execute
    (chat gates pre-dispatch via its approval map); this wrap is therefore only
    load-bearing on Daily voice.

    A denied/timed-out call never runs the real MCP round-trip (no upstream
    side effect): ``gate_call`` returns the bare ``{status, reason}`` denial —
    the same shape a denied voice GLOBAL function returns, so the LLM reads it
    uniformly. Only on approve does the real MCP ``{status, data}`` envelope
    flow through.
    """
    if approval is None or bot_instance is None:
        return handler

    async def gated_handler(args: Dict[str, Any], flow_manager: Any) -> Any:
        async def execute() -> Any:
            return await handler(args, flow_manager)

        return await gate_call(bot_instance, registered_name, approval, args, execute)

    return gated_handler


def _state_wrap_mcp_handler(handler: Any, bot_instance: Any, tool_name: str) -> Any:
    """Apply the voice SessionStatePolicy to an MCP tool handler.

    The MCP counterpart of ``_make_global_wrapper``'s state hook: inject
    state-driven args before the call and lift identifiers off the result
    after it, so a template's ``tool_arg_injection`` / ``state_reducers`` reach
    MCP tools on voice (chat already runs them in its ``_cycle_loop``). No-op
    without a bot, and skipped when the bot sets ``handles_state_externally``
    (chat). ``tool_name`` is the REGISTERED name the LLM calls — the name that
    injection/reducer rules target. Applied OUTSIDE the approval gate, so a
    denied call's bare ``{status, reason}`` simply finds no matching reducer
    paths (no-op), while an approved call reduces its real result.
    """
    if bot_instance is None:
        return handler

    async def state_handler(args: Dict[str, Any], flow_manager: Any) -> Any:
        if getattr(bot_instance, "handles_state_externally", False):
            return await handler(args, flow_manager)
        injected = _inject_voice_state(bot_instance, tool_name, args)
        result = await handler(injected, flow_manager)
        _reduce_voice_state(bot_instance, tool_name, result)
        return result

    return state_handler


def _deep_merge_defaults(
    args: Dict[str, Any], defaults: Dict[str, Any]
) -> Dict[str, Any]:
    """Deep-merge ``defaults`` into ``args``; caller-provided values win.

    Mirrors ``only_if_missing`` semantics in :func:`inject_tool_args`. New dict
    is returned (no mutation). Used to inject protocol-level metadata that
    every tool call on a server needs (e.g. Shopify UCP's
    ``arguments.meta.ucp-agent.profile``) without touching session-state-driven
    injection logic in the chat agent.
    """
    if not defaults:
        return dict(args)

    def merge(into: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in src.items():
            existing = into.get(k)
            if isinstance(v, dict) and isinstance(existing, dict):
                into[k] = merge(dict(existing), v)
            elif k not in into or into[k] is None:
                into[k] = v
        return into

    return merge(dict(args), defaults)


# Turn-scoped MCP client pool. Keys are stable server labels (server.name
# or template URL form) and values are live MCPClient instances whose
# StreamableHTTP connection + ClientSession + initialize handshake have
# already happened. The owning component (e.g. ChatAgent.run_turn) creates
# an empty dict at turn start and calls ``close_mcp_pool`` in its finally
# block. Handlers lazily acquire from the pool on first call and reuse on
# every subsequent call within the same turn, eliminating the per-call
# connect/initialize/notifications round-trip (~150–300 ms).
MCPPool = Dict[str, MCPClient]


async def _acquire_pooled_client(
    pool: MCPPool,
    key: str,
    server_params: StreamableHttpParameters,
) -> MCPClient:
    """Return a live MCPClient from the pool, opening + stashing on first miss.

    Tool calls within one chat turn execute sequentially (see
    ``ChatAgent._run_turn_inner``'s ``for call in tool_calls`` loop), so no
    lock is needed — at most one acquire is in flight per server at a time.
    """
    existing = pool.get(key)
    if existing is not None:
        return existing
    client = MCPClient(server_params=server_params)
    await client.start()
    pool[key] = client
    return client


async def close_mcp_pool(pool: Optional[MCPPool]) -> None:
    """Close every MCPClient in the pool. Safe to call with None / empty.

    Errors during close are swallowed (logged at WARNING) so one bad
    connection doesn't prevent the rest from being released — matches the
    behaviour of an ``async with`` cleanup when the body raises.
    """
    if not pool:
        return
    for key, client in list(pool.items()):
        try:
            await client.close()
        except Exception as e:
            logger.warning(f"[BUDDY_MCP] error closing pooled client {key!r}: {e}")
    pool.clear()


# NOTE: Uses private _tool_wrapper to integrate with FlowManager's global
# functions. Public register_tools(llm) bypasses FlowManager orchestration.
# Pin pipecat-ai version to guard against API changes.
def _create_direct_http_tool_handler(
    server_params: StreamableHttpParameters,
    tool_name: str,
    response_schema: Optional[Union[Dict[str, str], Literal["full"]]] = None,
    response_transforms: Optional[List[ResponseTransform]] = None,
    ui_hint: Optional[ToolUiHint] = None,
    default_args: Optional[Dict[str, Any]] = None,
) -> Any:
    """Direct JSON-RPC HTTP poster — bypasses pipecat MCPClient entirely.

    Used when the upstream rejects the standard MCP handshake (initialize +
    tools/list) but accepts standalone `tools/call` POSTs. Shopify UCP is
    the motivating case: it gates discovery on the per-call agent profile,
    so we declare tool schemas in the template (`server.tool_schemas`) and
    skip discovery entirely; each call is a stateless JSON-RPC POST with
    the profile attached at `arguments.meta.ucp-agent.profile` via
    `default_args`.

    Same return-shape contract as the pipecat-backed handler — `default_args`
    deep-merge and the shared result pipeline (response_schema projection,
    response_transforms, ui_hint injection) all apply.
    """

    async def handler(args: Dict[str, Any], flow_manager: Any) -> FlowResult:
        merged_args = _deep_merge_defaults(args, default_args or {})
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": merged_args},
        }
        # server_params.headers can be None in some MCP wirings; coalesce
        # to an empty dict so the subsequent setdefault calls always have
        # a target. Filter out any None values that may have been declared
        # — httpx's headers param expects str-str pairs.
        raw_headers = server_params.headers or {}
        headers: Dict[str, str] = {
            k: v for k, v in raw_headers.items() if v is not None
        }
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")

        timeout_s = (
            server_params.timeout.total_seconds() if server_params.timeout else 30.0
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(server_params.url, json=body, headers=headers)
        except Exception as e:
            logger.warning(f"[BUDDY_MCP] direct {tool_name!r} transport failed: {e}")
            return cast(
                FlowResult,
                {"status": "error", "data": f"MCP transport error: {e}"},
            )

        # Surface HTTP-level failures as structured envelopes before
        # attempting to parse JSON. Without this branch, a 502 with an
        # HTML body would raise ValueError on resp.json() and the LLM
        # would see a generic "MCP tool error: <ValueError>" instead of
        # "upstream returned 502" — making prod incidents harder to
        # debug AND blocking the LLM from any chance of recovery.
        if resp.status_code >= 400:
            body_preview = resp.text[:500]
            logger.warning(
                f"[BUDDY_MCP] direct {tool_name!r} HTTP {resp.status_code}: "
                f"{body_preview!r}"
            )
            return cast(
                FlowResult,
                {
                    "status": "error",
                    "status_code": resp.status_code,
                    "data": f"Upstream HTTP {resp.status_code}: {body_preview}",
                },
            )

        try:
            payload = resp.json()
        except Exception as e:
            logger.warning(
                f"[BUDDY_MCP] direct {tool_name!r} non-JSON 2xx response: {e}"
            )
            return cast(
                FlowResult,
                {"status": "error", "data": f"MCP response not JSON: {e}"},
            )

        if "error" in payload:
            err = payload["error"]
            return cast(
                FlowResult,
                {"status": "error", "data": json.dumps(err)},
            )

        # JSON-RPC tools/call result is `{content: [{type:'text', text:'<json>'}], ...}`.
        # Extract the text payload — that's what pipecat's wrapper hands the LLM.
        result_obj = payload.get("result", {})
        content = result_obj.get("content") or []
        text_block = next(
            (
                c.get("text")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ),
            None,
        )
        result: Any = text_block if text_block is not None else json.dumps(result_obj)
        # MCP tool-level error: a 2xx JSON-RPC response whose result carries
        # ``isError: true`` (distinct from the protocol-level ``error`` handled
        # above). Feed it as is_success=False so the pipeline skips projection /
        # transforms — otherwise a tool_response_schemas whitelist would strip
        # the error text and the LLM would see an empty object instead of the
        # failure reason. The envelope status stays "success" (unchanged): the
        # LLM reads the error from ``data``, exactly as before this PR.
        is_error = bool(result_obj.get("isError"))
        result = apply_result_pipeline_json_str(
            result,
            tool_name=tool_name,
            is_success=not is_error,
            response_schema=response_schema,
            response_transforms=response_transforms,
            ui_hint=ui_hint,
            args=args,
        )
        return cast(FlowResult, {"status": "success", "data": result})

    return handler


def _create_mcp_tool_handler(
    server_params: StreamableHttpParameters,
    tool_name: str,
    pool: Optional[MCPPool] = None,
    server_key: Optional[str] = None,
    response_schema: Optional[Union[Dict[str, str], Literal["full"]]] = None,
    response_transforms: Optional[List[ResponseTransform]] = None,
    ui_hint: Optional[ToolUiHint] = None,
    default_args: Optional[Dict[str, Any]] = None,
) -> Any:
    """Create a FlowManager-compatible handler for one MCP tool.

    When ``pool`` is provided (chat mode), the handler shares a single
    MCPClient with every other tool on the same server for the lifetime
    of the turn — the caller owns pool teardown via ``close_mcp_pool``.

    When ``pool`` is ``None`` (voice mode / call sites that haven't been
    plumbed through), the handler falls back to opening + closing a fresh
    MCPClient on every invocation — the pre-pool behaviour.

    The parsed tool result is run through the shared result pipeline
    (``apply_result_pipeline_json_str``): ``response_schema`` projection →
    ``response_transforms`` in-place mutation → ``ui_hint`` JIT UI injection,
    each applied only when its config is set. Channel-agnostic — the same
    code runs whether the caller is voice (pipecat FlowManager) or chat
    (ChatAgent), and it is the identical pipeline Global HTTP functions use.

    When ``default_args`` is non-empty, those values are deep-merged into
    the caller's ``args`` (caller wins on conflicts) before dispatch. This
    is how UCP protocol metadata like ``meta.ucp-agent.profile`` lands on
    every tool call without polluting per-tool injection rules.
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

        # Apply server-level default_args (protocol-level metadata like
        # UCP's meta.ucp-agent.profile). Channel-agnostic; runs before
        # dispatch in BOTH voice and chat paths.
        merged_args = _deep_merge_defaults(args, default_args or {})

        params = FunctionCallParams(
            function_name=tool_name,
            tool_call_id="",
            arguments=merged_args,
            llm=None,  # type: ignore[arg-type]
            context=None,  # type: ignore[arg-type]
            result_callback=result_callback,
        )

        # NOTE: both branches below pass is_success=True to the pipeline.
        # Pipecat's MCPClient flattens tool results to a text string and drops
        # the MCP ``isError`` flag (see pipecat mcp_service._call_tool), so this
        # discovery path cannot detect a tool-level error to gate projection on
        # — unlike the declared/UCP direct-HTTP handler above, which reads
        # isError off the JSON-RPC result. Known limitation: a
        # ``tool_response_schemas`` projection on a discovery-path tool that
        # returns an error body may strip it. Discovery-path projection is rare
        # (UCP is the motivating case and uses the direct handler); revisit if a
        # discovery tool needs error-aware projection.
        #
        # Pooled path — chat mode.
        if pool is not None and server_key is not None:
            try:
                client = await _acquire_pooled_client(pool, server_key, server_params)
                await asyncio.wait_for(client._tool_wrapper(params), timeout=30)
                result = await asyncio.wait_for(future, timeout=30)
                result = apply_result_pipeline_json_str(
                    result,
                    tool_name=tool_name,
                    is_success=True,
                    response_schema=response_schema,
                    response_transforms=response_transforms,
                    ui_hint=ui_hint,
                    args=args,
                )
                return cast(FlowResult, {"status": "success", "data": result})
            except asyncio.TimeoutError:
                # Evict so the next call in this turn opens a fresh client.
                await _evict_pooled_client(pool, server_key)
                return cast(FlowResult, {"status": "error", "data": "MCP tool timeout"})
            except Exception as e:
                await _evict_pooled_client(pool, server_key)
                logger.warning(f"[BUDDY_MCP] Tool '{tool_name}' failed: {e}")
                return cast(
                    FlowResult, {"status": "error", "data": f"MCP tool error: {e}"}
                )

        # Unpooled path — voice mode (and any caller that hasn't migrated).
        # Fresh MCPClient per call; matches the pre-0.7 behaviour exactly.
        try:
            async with MCPClient(server_params=server_params) as mcp_client:
                await asyncio.wait_for(mcp_client._tool_wrapper(params), timeout=30)
            result = await asyncio.wait_for(future, timeout=30)
            result = apply_result_pipeline_json_str(
                result,
                tool_name=tool_name,
                is_success=True,
                response_schema=response_schema,
                response_transforms=response_transforms,
                ui_hint=ui_hint,
                args=args,
            )
            return cast(FlowResult, {"status": "success", "data": result})
        except asyncio.TimeoutError:
            return cast(FlowResult, {"status": "error", "data": "MCP tool timeout"})
        except Exception as e:
            logger.warning(f"[BUDDY_MCP] Tool '{tool_name}' failed: {e}")
            return cast(FlowResult, {"status": "error", "data": f"MCP tool error: {e}"})

    return handler


async def _evict_pooled_client(pool: MCPPool, key: str) -> None:
    """Drop a pooled client after a failed call so the next acquire reopens.

    A streaming-HTTP MCP connection can be half-broken (server closed the
    stream, network blip mid-response) where the client object survives
    but subsequent ``call_tool`` requests time out. Evicting on any failure
    keeps the rest of the turn usable at the cost of one fresh connect.
    """
    bad = pool.pop(key, None)
    if bad is None:
        return
    try:
        await bad.close()
    except Exception as e:
        logger.warning(f"[BUDDY_MCP] error closing evicted client {key!r}: {e}")


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


async def _build_server_params(
    server: McpServerConfig,
    template_vars: Dict[str, Any],
) -> StreamableHttpParameters:
    """Resolve a server config + template_vars into StreamableHttpParameters.

    Shared by the voice loader (per-call clients) and the chat session pool
    (per-turn persistent clients). Substitutes ``{variable}`` placeholders in
    the URL and auth fields from ``template_vars``.

    SECURITY: the resolved URL is a template-controlled destination that we are
    about to attach decrypted tenant credentials to (via ``_build_auth_headers``)
    and hit at flow-build time. It MUST pass the shared SSRF egress guard first —
    https-only, no internal/loopback/link-local/metadata targets — so it cannot
    be pointed at cloud metadata or an internal service (PT-03). Validation
    happens before any credential header is built.
    """
    resolved_url = _resolve_placeholders(server.url, template_vars)
    await validate_egress_url(resolved_url)
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
    bot_instance: Any = None,
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
    server_params = await _build_server_params(server, template_vars)
    # Prefer the stable name; fall back to the raw template URL (with
    # placeholders) rather than the resolved URL to avoid logging
    # customer-identifying substitutions.
    label = server.name or server.url
    logger.info(f"[BUDDY_MCP] Connecting to {label}")

    # Declared-schemas path (UCP): skip the pipecat MCPClient handshake
    # entirely. Each tool registers a direct-HTTP poster bound to server.url.
    #
    # Truthiness pitfall guard: `if server.tool_schemas:` would treat an
    # empty list `[]` as "no schemas declared" and silently misroute to
    # the discovery branch — which against a UCP endpoint fails on
    # `initialize`. We branch on `is not None` so a deliberately-empty
    # declaration is honored (and warned about) instead.
    if server.tool_schemas is not None:
        if not server.tool_schemas:
            logger.warning(
                f"[BUDDY_MCP] {label}: tool_schemas is declared but empty — "
                "skipping discovery (no tools will be registered for this server). "
                "Either populate tool_schemas or remove the field entirely to "
                "fall back to MCPClient discovery."
            )
            return []
        functions: List[FlowsFunctionSchema] = []
        for schema in server.tool_schemas:
            tool_name = schema["name"]
            if tool_name in existing_names and server.name:
                tool_name = f"{server.name}_{tool_name}"
            tool_transforms = (
                server.tool_response_transforms.get(schema["name"]) or None
            )
            tool_ui_hint = server.tool_ui_instructions.get(schema["name"])
            tool_response_schema = (
                server.tool_response_schemas.get(schema["name"]) or None
            )
            # HITL: per-tool approval is authored against the RAW upstream
            # name (like transforms/ui), but gated under the REGISTERED name.
            tool_approval = server.tool_approvals.get(schema["name"])
            handler = _gate_mcp_handler(
                _create_direct_http_tool_handler(
                    server_params,
                    schema["name"],
                    response_schema=tool_response_schema,
                    response_transforms=tool_transforms,
                    ui_hint=tool_ui_hint,
                    default_args=server.default_args,
                ),
                bot_instance,
                tool_name,
                tool_approval,
            )
            handler = _state_wrap_mcp_handler(handler, bot_instance, tool_name)
            schema_kwargs: Dict[str, Any] = {}
            if tool_approval is not None:
                schema_kwargs["timeout_secs"] = _mcp_approval_timeout_secs(
                    tool_approval
                )
            functions.append(
                FlowsFunctionSchema(
                    name=tool_name,
                    description=schema.get("description", ""),
                    properties=schema.get("properties", {}),
                    required=schema.get("required", []),
                    handler=handler,
                    **schema_kwargs,
                )
            )
            existing_names.add(tool_name)
            logger.info(
                f"[BUDDY_MCP] Registered (declared) tool: {tool_name} (from {label})"
                + (" [approval-gated]" if tool_approval is not None else "")
            )
        logger.info(f"[BUDDY_MCP] Loaded {len(functions)} declared tools from {label}")
        return functions

    # Discovery path (legacy MCP): handshake with the server and fetch the
    # tool schema dynamically. Used when ``server.tool_schemas`` isn't set
    # — UCP servers go through the declared-schemas branch above and never
    # reach this code.
    async with MCPClient(server_params=server_params) as temp_client:
        tools_schema = await asyncio.wait_for(
            temp_client.get_tools_schema(),
            timeout=server.timeout,
        )

    discovered_functions: List[FlowsFunctionSchema] = []
    for func_schema in tools_schema.standard_tools:
        # Prefix tool name with server name only on collision
        tool_name = func_schema.name
        if tool_name in existing_names and server.name:
            tool_name = f"{server.name}_{tool_name}"

        # Look up per-tool transforms by the RAW MCP tool name (templates
        # author against the upstream name; the local prefix is internal).
        tool_transforms = server.tool_response_transforms.get(func_schema.name) or None
        tool_ui_hint = server.tool_ui_instructions.get(func_schema.name)
        tool_response_schema = (
            server.tool_response_schemas.get(func_schema.name) or None
        )
        tool_approval = server.tool_approvals.get(func_schema.name)
        handler = _gate_mcp_handler(
            _create_mcp_tool_handler(
                server_params,
                func_schema.name,
                response_schema=tool_response_schema,
                response_transforms=tool_transforms,
                ui_hint=tool_ui_hint,
                default_args=server.default_args,
            ),
            bot_instance,
            tool_name,
            tool_approval,
        )
        handler = _state_wrap_mcp_handler(handler, bot_instance, tool_name)
        schema_kwargs: Dict[str, Any] = {}
        if tool_approval is not None:
            schema_kwargs["timeout_secs"] = _mcp_approval_timeout_secs(tool_approval)

        discovered_functions.append(
            FlowsFunctionSchema(
                name=tool_name,
                description=func_schema.description,
                properties=func_schema.properties,
                required=func_schema.required,
                handler=handler,
                **schema_kwargs,
            )
        )
        # Track the chosen name so a subsequent tool from the same server
        # with a duplicate name also gets prefixed.
        existing_names.add(tool_name)
        logger.info(
            f"[BUDDY_MCP] Registered tool: {tool_name} (from {label})"
            + (" [approval-gated]" if tool_approval is not None else "")
        )

    logger.info(f"[BUDDY_MCP] Loaded {len(discovered_functions)} tools from {label}")
    return discovered_functions


async def get_mcp_global_functions(
    mcp_config: McpConfig,
    template_vars: Dict[str, str] | None = None,
    bot_instance: Any = None,
) -> List[FlowsFunctionSchema]:
    """Fetch tools from all enabled MCP servers and return as FlowManager global functions.

    Each tool handler creates a fresh MCPClient per invocation, so no shared
    client cleanup is needed at the call level.

    ``bot_instance`` (the voice agent) is threaded into the tool handlers so an
    MCP tool with a ``tool_approvals`` entry can reach the bot's
    ApprovalManager and gate in-process before its real round-trip (Pattern C).
    Chat uses :func:`get_mcp_global_functions_cached`, which gates pre-dispatch
    instead and needs no bot reference here.
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
                _load_server_tools(
                    server, template_vars, existing_names, bot_instance=bot_instance
                )
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
    mcp_pool: Optional[MCPPool] = None,
) -> Tuple[List[FlowsFunctionSchema], Dict[str, ApprovalConfig]]:
    """Cache-aware variant for chat mode.

    Reads tool metadata from Redis (per-template + URL hash, see
    ``mcp/cache.py``) and rebuilds ``FlowsFunctionSchema`` using
    ``_create_mcp_tool_handler``. Auth headers are rebuilt from
    ``template_vars`` on every call so credential rotation takes effect on
    the next turn without cache invalidation.

    When ``mcp_pool`` is provided the resulting handlers share one
    MCPClient per server for the lifetime of the turn. The caller owns
    pool teardown via ``close_mcp_pool``.

    Returns ``(functions, approval_map)`` where ``approval_map`` maps each
    gated MCP tool's REGISTERED name (raw, or ``<server.name>_<name>`` on a
    cross-server collision) to its ApprovalConfig. Chat does NOT wrap the
    handlers (unlike voice): it gates pre-dispatch by merging this map into
    ChatAgent._approval_map, so the existing name-keyed partition / pending
    row / approval-endpoint / resume machinery gates MCP tools unchanged.
    """
    all_functions: List[FlowsFunctionSchema] = []
    approval_map: Dict[str, ApprovalConfig] = {}

    for server in mcp_config.servers:
        if not server.enabled:
            continue

        try:
            server_params = await _build_server_params(server, template_vars)
        except Exception as e:
            logger.error(
                f"[BUDDY_MCP] chat: failed to build server params for "
                f"{server.name or server.url}: {type(e).__name__}: {e}"
            )
            continue

        # Stable label: prefer server.name; fall back to the raw template
        # URL (placeholder form) rather than the resolved URL.
        label = server.name or server.url

        # Declared-schemas path (UCP): skip discovery entirely. Match the
        # `is not None` semantics in `_load_server_tools` (line 551) — an
        # empty list MUST short-circuit, not fall through to discovery,
        # which would handshake against a UCP endpoint that has no MCP
        # initialize support and fail.
        if server.tool_schemas is not None:
            if not server.tool_schemas:
                logger.warning(
                    f"[BUDDY_MCP] chat: {label}: tool_schemas declared but empty — "
                    "no tools registered for this server (skipping discovery)."
                )
                continue
            existing_names = {f.name for f in all_functions}
            for schema in server.tool_schemas:
                tool_name = schema["name"]
                if tool_name in existing_names and server.name:
                    tool_name = f"{server.name}_{tool_name}"
                tool_transforms = (
                    server.tool_response_transforms.get(schema["name"]) or None
                )
                tool_ui_hint = server.tool_ui_instructions.get(schema["name"])
                tool_response_schema = (
                    server.tool_response_schemas.get(schema["name"]) or None
                )
                tool_approval = server.tool_approvals.get(schema["name"])
                if tool_approval is not None:
                    approval_map[tool_name] = tool_approval
                all_functions.append(
                    FlowsFunctionSchema(
                        name=tool_name,
                        description=schema.get("description", ""),
                        properties=schema.get("properties", {}),
                        required=schema.get("required", []),
                        handler=_create_direct_http_tool_handler(
                            server_params,
                            schema["name"],
                            response_schema=tool_response_schema,
                            response_transforms=tool_transforms,
                            ui_hint=tool_ui_hint,
                            default_args=server.default_args,
                        ),
                    )
                )
                existing_names.add(tool_name)
                logger.debug(
                    f"[BUDDY_MCP] chat: registered (declared) tool: {tool_name} (from {label})"
                )
            logger.info(
                f"[BUDDY_MCP] chat: loaded {len(server.tool_schemas)} declared tools from {label}"
            )
            continue

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
            tool_transforms = server.tool_response_transforms.get(meta["name"]) or None
            tool_ui_hint = server.tool_ui_instructions.get(meta["name"])
            tool_response_schema = (
                server.tool_response_schemas.get(meta["name"]) or None
            )
            tool_approval = server.tool_approvals.get(meta["name"])
            if tool_approval is not None:
                approval_map[tool_name] = tool_approval
            all_functions.append(
                FlowsFunctionSchema(
                    name=tool_name,
                    description=meta["description"],
                    properties=meta["properties"],
                    required=meta["required"],
                    handler=_create_mcp_tool_handler(
                        server_params,
                        meta["name"],
                        pool=mcp_pool,
                        server_key=label,
                        response_schema=tool_response_schema,
                        response_transforms=tool_transforms,
                        ui_hint=tool_ui_hint,
                        default_args=server.default_args,
                    ),
                )
            )
            existing_names.add(tool_name)
            logger.debug(
                f"[BUDDY_MCP] chat: registered tool: {tool_name} (from {label})"
            )

    logger.info(f"[BUDDY_MCP] chat: total tools loaded: {len(all_functions)}")
    return all_functions, approval_map
