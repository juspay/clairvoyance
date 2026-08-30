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

import asyncio
import json
from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.rtvi import SseRtviForwarder
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    FieldResolver,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.tool_pipeline import (
    apply_result_pipeline,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldSource,
    GlobalHttpFunction,
    SseResponseMode,
)
from app.core.logger import logger


def _retry_condition_met(response_body: str, cfg: Any) -> bool:
    """True when the parsed body's `cfg.field` (dotted) equals `cfg.equals`.

    Unparseable / non-dict bodies and missing fields return True — never
    keep re-requesting something we cannot evaluate.
    """
    try:
        current: Any = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        return True
    for key in cfg.field.split("."):
        if not isinstance(current, dict) or key not in current:
            return True
        current = current[key]
    return bool(current == cfg.equals)


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

        # Step 4b: content-conditional re-request (poll-until-ready APIs —
        # see RetryUntilConfig). Bounded and invisible to the LLM: only the
        # final response continues down the pipeline. Non-SSE 2xx only.
        retry_until = config.http_request.retry_until
        if retry_until is not None:
            attempt = 1
            while (
                attempt < retry_until.max_attempts
                and result is not None
                and result != (0, "")
                and not sse_forwarder.chunks
                and 200 <= result[0] < 300
                and not _retry_condition_met(result[1], retry_until)
            ):
                logger.info(
                    f"[{function_name}] retry_until: {retry_until.field!r} not "
                    f"{retry_until.equals!r} yet — attempt "
                    f"{attempt + 1}/{retry_until.max_attempts} after "
                    f"{retry_until.delay_ms}ms"
                )
                await asyncio.sleep(retry_until.delay_ms / 1000)
                result = await executor.execute(
                    config=config.http_request,
                    resolved_fields=resolved_fields,
                    fire_and_forget=False,
                    on_sse_event=sse_forwarder,
                )
                attempt += 1

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

        # Centralized result pipeline (shared with MCP tools via
        # handlers/transport/utils/tool_pipeline.py): projection
        # (expected_response_schema) → transforms (response_transforms) →
        # ui-hint. Projection + transforms are 2xx-only so error bodies pass
        # through raw and the LLM can read messages/codes ("person not found")
        # verbatim; ui-hint honors its own trigger. Keeping the three in one
        # chokepoint stops the HTTP + MCP seams from drifting apart again.
        data = apply_result_pipeline(
            data,
            tool_name=function_name,
            is_success=is_success,
            response_schema=config.expected_response_schema,
            response_transforms=config.response_transforms,
            ui_hint=config.ui_hint,
            args=args,
        )

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
                # ui_hint splices _ui_* directives into `data` for the LLM; keep
                # them out of the persisted node-traversal record so analytics /
                # transcripts hold only the real response fields. No-op (same
                # object) when ui_hint is unset.
                response_to_store: Optional[Dict[str, Any]] = (
                    {k: v for k, v in data.items() if not k.startswith("_ui_")}
                    if config.ui_hint
                    else data
                )
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
