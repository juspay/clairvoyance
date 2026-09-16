"""
HTTP/MCP fetch and response-matching helpers for the ``external_api`` pre-check
path -- fetching a response, projecting ``export_to_payload`` fields out of it,
and comparing a field against the configured expectation.
"""

import json
from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.tool_pipeline import (
    apply_result_pipeline,
)
from app.ai.voice.agents.breeze_buddy.mcp import (
    _build_server_params,
    _create_direct_http_tool_handler,
)
from app.ai.voice.agents.breeze_buddy.template.types import HttpAuthConfig, HttpAuthType
from app.core.logger import logger
from app.schemas import PreCheckConfig, PreCheckMatchType


def _value_present(actual: Any, needle: Any) -> bool:
    """True if ``needle`` is present inside ``actual`` (case-insensitive).

    Supports both lists (membership / per-item substring) and plain strings
    (substring). Shopify ``tags`` may arrive as a list or a comma-separated
    string, so this handles both.
    """
    if actual is None:
        return False
    needle_s = str(needle).lower()
    if isinstance(actual, (list, tuple, set)):
        return any(needle_s in str(item).lower() for item in actual)
    return needle_s in str(actual).lower()


def _value_matches(actual: Any, expected: Any, match_type: PreCheckMatchType) -> bool:
    """Apply the pre-check's match_type, returning whether the call should PROCEED."""
    if match_type == PreCheckMatchType.NOT_EQUALS:
        return actual != expected
    if match_type == PreCheckMatchType.CONTAINS:
        return _value_present(actual, expected)
    if match_type == PreCheckMatchType.NOT_CONTAINS:
        return not _value_present(actual, expected)
    if match_type in (PreCheckMatchType.GT, PreCheckMatchType.LT):
        try:
            a, e = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return a > e if match_type == PreCheckMatchType.GT else a < e
    # EQUALS (default)
    return actual == expected


def _extract_exports(
    response_data: Any,
    fields: Dict[str, str],
    pre_check_name: str,
) -> Dict[str, str]:
    """Project ``export_to_payload`` fields out of a pre-check response.

    Projection only — an ``mcp`` pre-check's values are already shaped by the
    server's ``tool_response_transforms``.

    Best-effort by contract — the caller must not let a failure here change
    the go/no-go outcome, so every error path returns ``{}``.
    """
    extracted = apply_result_pipeline(
        response_data,
        tool_name=f"pre-check:{pre_check_name}",
        response_schema=fields,
    )
    if extracted is response_data:
        return {}
    if not isinstance(extracted, dict):
        return {}

    return {
        key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for key, value in extracted.items()
        if key in fields and value not in (None, "", [], {})
    }


async def _fetch_mcp_response(
    pre_check: PreCheckConfig,
    context: Dict[str, Any],
    executor: HttpRequestExecutor,
) -> Tuple[Optional[Any], Optional[str]]:
    """Call an MCP tool through the same handler the conversation uses, which
    brings the ``default_args`` merge, the envelope unwrap and the per-tool
    transforms with it.

    Returns ``(payload, None)`` or ``(None, reason)``; ``reason`` feeds
    ``_apply_default`` so ``default_on_failure`` still decides.
    """
    server = pre_check.mcp
    tool = pre_check.mcp_tool
    if server is None or not tool:
        return None, "'mcp' is set but 'mcp_tool' is missing"

    arguments = executor._resolve_body(pre_check.mcp_arguments, context)
    if not isinstance(arguments, dict):
        return None, "mcp_arguments did not resolve to an object"

    # _build_server_params runs the shared SSRF egress guard on the resolved URL
    # before it attaches any decrypted credential header, so awaiting it here is
    # the validation — the separate pre-flight check this replaces called a
    # method that no longer exists. SSRFError subclasses ValueError, so a
    # rejected destination still degrades into a reason for _apply_default
    # rather than propagating out of the pre-check.
    try:
        server_params = await _build_server_params(server, context)
    except ValueError as e:
        return None, f"MCP server URL rejected: {e}"

    handler = _create_direct_http_tool_handler(
        server_params,
        tool,
        response_schema=server.tool_response_schemas.get(tool) or None,
        response_transforms=server.tool_response_transforms.get(tool) or None,
        default_args=server.default_args,
    )

    logger.info(
        f"Pre-check '{pre_check.name}': calling MCP tool "
        f"'{tool}' on server '{server.name or server.url}'"
    )
    envelope = await handler(arguments, None)

    if envelope.get("status") != "success":
        return None, f"MCP tool failed: {envelope.get('data')}"

    data = envelope.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data), None
        except json.JSONDecodeError:
            # Prose answer — nothing to project out of it.
            return None, "MCP tool returned non-JSON content"
    return data, None


def _convert_auth_dict_to_config(
    auth_dict: Optional[Dict[str, Any]],
) -> Optional[HttpAuthConfig]:
    """
    Convert a raw auth dict from PreCheckHttpRequest to HttpAuthConfig.

    Expected dict structure:
    {
        "type": "bearer" | "basic" | "api_key",
        "token": "...",           # for bearer
        "username": "...",        # for basic
        "password": "...",        # for basic
        "api_key_name": "...",    # for api_key
        "api_key_value": "..."    # for api_key
    }
    """
    if not auth_dict:
        return None

    try:
        auth_type_str = auth_dict.get("type", "").lower()
        if not auth_type_str:
            logger.warning("Auth dict missing 'type' field, skipping auth config")
            return None

        # Map string to HttpAuthType enum
        type_mapping = {
            "bearer": HttpAuthType.BEARER,
            "basic": HttpAuthType.BASIC,
            "api_key": HttpAuthType.API_KEY,
        }

        auth_type = type_mapping.get(auth_type_str)
        if not auth_type:
            logger.warning(f"Unknown auth type '{auth_type_str}', skipping auth config")
            return None

        return HttpAuthConfig(
            type=auth_type,
            token=auth_dict.get("token"),
            username=auth_dict.get("username"),
            password=auth_dict.get("password"),
            api_key_name=auth_dict.get("api_key_name"),
            api_key_value=auth_dict.get("api_key_value"),
        )
    except Exception as e:
        logger.warning(f"Failed to convert auth dict to HttpAuthConfig: {e}")
        return None


def _convert_query_params_to_str_dict(
    params: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Convert query params dict to Dict[str, str] as required by HttpRequestConfig.
    Filters out None values and converts all values to strings.
    """
    if not params:
        return {}

    result: Dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        result[key] = str(value)
    return result
