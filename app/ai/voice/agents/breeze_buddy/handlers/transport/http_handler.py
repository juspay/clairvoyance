"""
HTTP Function Handler for Global Functions.

Enables LLM to make HTTP requests and receive actual API responses.
This handler blocks and waits for the HTTP response, then returns the
actual data to the LLM for decision-making.

Contrast with hooks (async):
- Hooks: Fire-and-forget, don't return data to LLM
- Handlers: Block, wait for response, return data to LLM

Uses the same resolution pattern as hooks:
- expected_fields with source: static/llm
- {placeholder} resolution in http_request config
"""

import json
from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    FieldResolver,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldSource,
    GlobalHttpFunction,
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
    2. Executes HTTP request with placeholder resolution
    3. Waits for response (fire_and_forget=False)
    4. Parses response as JSON (falls back to raw text)
    5. Returns data to LLM

    Note: Response size is limited by HTTP_REQUEST_MAX_RESPONSE_BYTES in the
    http_requester module. Oversized responses are rejected before reaching this handler.

    Args:
        context: TemplateContext with bot state access (includes aiohttp_session)
        args: LLM function arguments (e.g., {"order_id": "12345"})
        function_config: GlobalHttpFunction configuration from template

    Returns:
        Tuple of (result_dict, None)
        - result_dict: Response data for LLM with status, status_code, data fields
        - None: Always None (stay on current node, no transition)
    """
    # Validate function_config is provided
    if function_config is None:
        logger.error("[http_function_handler] function_config is required but was None")
        return {
            "status": "error",
            "error": "Function configuration not provided",
        }, None

    # Type narrowing: after the None check, function_config is guaranteed to be GlobalHttpFunction
    config: GlobalHttpFunction = function_config

    function_name = config.name
    logger.info(f"[http_function_handler] Starting HTTP call for '{function_name}'")

    # Validate we have aiohttp session
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

        # Step 1: Resolve expected_fields using FieldResolver (same pattern as hooks)
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
                    # Log field resolution without exposing potentially sensitive values
                    logger.debug(
                        f"[{function_name}] Resolved field '{field_name}' "
                        f"from source '{field_cfg.source}' (value_length={len(str(resolved_value))})"
                    )

        logger.debug(
            f"[{function_name}] Resolved fields for HTTP request: {list(resolved_fields.keys())}"
        )

        # Step 2: Create executor and execute HTTP request (wait for response)
        executor = HttpRequestExecutor(session=context.aiohttp_session)

        logger.info(
            f"[{function_name}] Executing HTTP {config.http_request.method.value} "
            f"request to {config.http_request.url}"
        )

        result = await executor.execute(
            config=config.http_request,
            resolved_fields=resolved_fields,
            fire_and_forget=False,  # KEY: Wait for response
        )

        # Step 3: Handle response
        if result is None or result == (0, ""):
            logger.error(f"[{function_name}] HTTP request failed after retries")
            return {
                "status": "error",
                "error": "Request failed after retries",
            }, None

        status_code, response_body = result

        logger.info(
            f"[{function_name}] HTTP response received: status={status_code}, "
            f"body_length={len(response_body)} bytes"
        )

        # Step 4: Try to parse JSON, fallback to raw text
        try:
            data = json.loads(response_body)
            logger.debug(
                f"[{function_name}] Parsed JSON response with "
                f"{len(data) if isinstance(data, dict) else 'non-dict'} keys"
            )
        except json.JSONDecodeError:
            logger.debug(
                f"[{function_name}] Response is not valid JSON, using raw text"
            )
            data = response_body

        # Step 5: Return formatted response to LLM
        is_success = 200 <= status_code < 300
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
