"""
HTTP Function Handler for Global Functions.

Enables LLM to make HTTP requests and receive actual API responses.
This handler blocks and waits for the HTTP response, then returns the
actual data to the LLM for decision-making.

SSE responses (Content-Type: text/event-stream) are auto-detected:
each event is forwarded to the frontend via RTVI in real-time via
``SseRtviForwarder``. What the LLM sees on return is controlled by
``sse_response_handler`` on the ``GlobalHttpFunction`` config.

Contrast with hooks (async):
- Hooks: Fire-and-forget, don't return data to LLM
- Handlers: Block, wait for response, return data to LLM

Uses the same resolution pattern as hooks:
- expected_fields with source: static/llm
- {placeholder} resolution in http_request config
"""

import copy
import json
from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.rtvi import SseRtviForwarder
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    FieldResolver,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_filter import (
    apply_response_schema,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
    apply_response_transforms,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldSource,
    GlobalHttpFunction,
    SseResponseMode,
)
from app.core.logger import logger


async def http_function_handler(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalHttpFunction] = None,
) -> Tuple[Dict[str, Any], None]:
    """
    Execute global HTTP function and return response to LLM.

    This handler:
    1. Resolves expected_fields using FieldResolver (same as hooks)
    2. Executes the HTTP request via HttpRequestExecutor
    3. SSE responses (text/event-stream) are auto-detected — each event
       is forwarded to the frontend via RTVI, only the selected event
       goes to the LLM.
    4. Parses response as JSON (falls back to raw text)
    5. Returns data to LLM

    Args:
        context: TemplateContext with bot state access (includes aiohttp_session)
        args: LLM function arguments (e.g., {"order_id": "12345"})
        function_config: GlobalHttpFunction configuration from template

    Returns:
        Tuple of (result_dict, None)
        - result_dict: Response data for LLM with status, status_code, data fields
        - None: Always None (stay on current node, no transition)
    """
    if function_config is None:
        logger.error("[http_function_handler] function_config is required but was None")
        return {
            "status": "error",
            "error": "Function configuration not provided",
        }, None

    config: GlobalHttpFunction = function_config
    function_name = config.name
    logger.info(f"[http_function_handler] Starting HTTP call for '{function_name}'")

    if not context.aiohttp_session:
        logger.error(f"[{function_name}] No aiohttp_session available in context")
        return {
            "status": "error",
            "error": "HTTP session not available",
        }, None

    try:
        # Pre-flight validation: Check all LLM-sourced fields are present in args
        missing_args = []
        for field_name, field_cfg in config.expected_fields.items():
            if field_cfg.source == FieldSource.LLM:
                arg_name = field_cfg.value or field_name
                if arg_name and arg_name not in args:
                    missing_args.append(arg_name)

        if missing_args:
            logger.error(
                f"[{function_name}] Missing required LLM arguments: {missing_args}"
            )
            return {
                "status": "error",
                "error": f"Missing required arguments: {', '.join(missing_args)}",
            }, None

        # Step 1: Resolve expected_fields using FieldResolver
        resolver = FieldResolver(context=context, args=args)
        resolved_fields: Dict[str, Any] = {}

        if config.expected_fields:
            logger.debug(
                f"[{function_name}] Resolving {len(config.expected_fields)} expected_fields"
            )
            for field_name, field_cfg in config.expected_fields.items():
                resolved_value = resolver.resolve_value(
                    field_cfg, field_name=field_name
                )
                if resolved_value is not None:
                    resolved_fields[field_name] = resolved_value

        logger.debug(
            f"[{function_name}] Resolved fields: {list(resolved_fields.keys())}"
        )

        # Step 2: Create executor
        executor = HttpRequestExecutor(session=context.aiohttp_session)

        logger.info(
            f"[{function_name}] Executing HTTP {config.http_request.method.value} "
            f"request to {config.http_request.url}"
        )

        # Step 3: SSE events stream to the client automatically via the forwarder;
        # sse_response_handler only shapes what the LLM sees on return.
        sse_forwarder = SseRtviForwarder(context, function_name)

        # Step 4: Execute request (SSE auto-detected from Content-Type)
        result = await executor.execute(
            config=config.http_request,
            resolved_fields=resolved_fields,
            fire_and_forget=False,
            on_sse_event=sse_forwarder,
        )

        # Step 5: Handle response
        if result is None or result == (0, ""):
            logger.error(f"[{function_name}] HTTP request failed after retries")
            return {
                "status": "error",
                "error": "Request failed after retries",
            }, None

        status_code, response_body = result

        logger.info(
            f"[{function_name}] HTTP response: status={status_code}, "
            f"body_length={len(response_body)}"
        )

        # Step 6: If SSE events were collected, build LLM payload
        chunks = sse_forwarder.chunks
        if chunks:
            logger.info(f"[{function_name}] SSE response: {len(chunks)} chunks")
            sse_forwarder.emit_end()
            handler_cfg = config.sse_response_handler
            if handler_cfg and handler_cfg.mode == SseResponseMode.SELECT:
                idx = handler_cfg.select_index
                try:
                    if idx is None:
                        raise IndexError("select_index not set")
                    data = chunks[idx].get("data", "")
                    logger.info(f"[{function_name}] SSE select[{idx}] returned to LLM")
                except IndexError as e:
                    logger.warning(
                        f"[{function_name}] invalid select_index={idx} "
                        f"for {len(chunks)} chunks: {e}"
                    )
                    return {
                        "status": "error",
                        "status_code": status_code,
                        "error": "Something went wrong",
                    }, None
            else:
                # None / FULL: concatenated data strings from the executor.
                data = response_body
                logger.debug(
                    f"[{function_name}] SSE full mode: {len(response_body)} bytes to LLM"
                )
        else:
            # Step 7: Standard response — parse JSON, fallback to raw text
            try:
                data = json.loads(response_body)
                logger.debug(f"[{function_name}] parsed JSON response")
            except json.JSONDecodeError:
                data = response_body
                logger.debug(f"[{function_name}] non-JSON response, using raw text")

        is_success = 200 <= status_code < 300

        # Apply expected_response_schema: extract only whitelisted fields
        # before returning data to the LLM. Only applied on successful (2xx)
        # responses — error bodies pass through unfiltered so the LLM can
        # read error messages/codes (e.g. "person not found") as-is.
        if (
            is_success
            and config.expected_response_schema
            and config.expected_response_schema != "full"
            and isinstance(data, (dict, list))
        ):
            data = apply_response_schema(
                data, config.expected_response_schema, args=args
            )
            logger.debug(
                f"[{function_name}] response filtered via expected_response_schema: "
                f"{list(config.expected_response_schema.keys())}"
            )

        # Response transforms — applied after projection so templates can
        # both narrow AND mutate. Channel-agnostic: voice + chat both
        # dispatch through this handler. Only 2xx — error bodies stay raw
        # so the LLM can read them verbatim.
        #
        # ``apply_response_transforms`` mutates in place; deep-copy first
        # so a mid-loop exception leaves the original ``data`` intact
        # rather than handing the LLM a partially-transformed payload.
        if is_success and config.response_transforms and isinstance(data, (dict, list)):
            try:
                transformed = copy.deepcopy(data)
                apply_response_transforms(transformed, config.response_transforms)
                data = transformed
            except Exception as e:
                logger.warning(f"[{function_name}] response_transforms failed: {e}")

        logger.debug(f"[{function_name}] data going to LLM: {data}")

        # Record global function call in node traversal path.
        # Storage rules (controlled by expected_response_schema on the function):
        #
        #   Dict of JMESPath expressions  → store the already-filtered dict on 2xx;
        #                                   store raw error body on 4xx/5xx
        #   "full"                         → store the full raw response on 2xx;
        #                                   store raw error body on 4xx/5xx
        #   Empty {} (default / not set)   → store nothing
        #
        # For non-JSON error responses (e.g. plain "Not found" text on 404),
        # wrap in a dict so they are always storable.
        if config.expected_response_schema:
            if isinstance(data, dict):
                response_to_store: Optional[Dict[str, Any]] = data
            elif is_success:
                # 2xx non-dict (e.g. plain string or list) — wrap so it can be persisted
                response_to_store = {"response": data, "status_code": status_code}
            else:
                # Non-JSON error body — wrap so it can be persisted
                response_to_store = {"error": data, "status_code": status_code}
        else:
            response_to_store = None  # no schema — never store
        context.record_global_function_call(
            function_name=function_name,
            function_args=args,
            function_response=response_to_store,
        )

        return {
            "status": "success" if is_success else "error",
            "status_code": status_code,
            "data": data,
        }, None

    except Exception as e:
        logger.error(
            f"[{function_name}] Exception in HTTP function handler: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "error": str(e),
        }, None
