"""
Pre-check executor for validating whether a call should proceed.

Runs configured pre-checks (e.g., external API calls) before a call is initiated
from the backlog. Each pre-check returns a go/no-go decision.

Pre-checks are configured per merchant/template in the call_execution_config table.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.services.credential_auth import (
    resolve_credential_auth,
    resolve_credential_scope,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthConfig,
    HttpAuthType,
    HttpMethod,
    HttpRequestConfig,
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credential_by_id,
)
from app.schemas import (
    Credential,
    LeadCallTracker,
    PreCheckConfig,
    PreCheckDefaultAction,
    PreCheckMatchType,
    PreCheckType,
)


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


@dataclass
class SinglePreCheckResult:
    """Result of a single pre-check execution."""

    name: str
    passed: bool
    reason: str
    response_data: Optional[Dict[str, Any]] = None


@dataclass
class PreCheckResult:
    """Aggregated result of all pre-checks for a lead."""

    should_proceed: bool
    results: List[SinglePreCheckResult] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary for logging."""
        parts = []
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            parts.append(f"{r.name}: {status} ({r.reason})")
        return "; ".join(parts)


async def _build_resolution_context(
    lead: LeadCallTracker,
    template: Optional[TemplateModel],
    credential_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the placeholder resolution context by merging credentials, template secrets, and payload.

    Resolution order (later overrides earlier):
    1. Credentials from credentials table (by credential_id if provided)
    2. Template secrets (API keys, tokens, base URLs)
    3. Lead payload (customer data, order data)
    4. Core lead fields (reseller_id, lead_id, etc.)
    """
    context: Dict[str, Any] = {}

    # 1. Credentials from credentials table (by credential_id)
    if credential_id:
        try:
            credential = await get_credential_by_id(credential_id, mask=False)
            if credential and credential.is_active and credential.value:
                context.update(credential.value)
                logger.info(
                    f"Loaded credentials by ID {credential_id}: {list(credential.value.keys())}"
                )
            else:
                logger.warning(
                    f"Credential {credential_id} not found, inactive, or empty"
                )
        except Exception as e:
            logger.warning(f"Failed to load credentials for pre-check context: {e}")

    # 2. Template secrets (override credentials for same keys)
    if template and template.secrets:
        context.update(template.secrets)

    # 3. Lead payload (override both)
    if lead.payload:
        context.update(lead.payload)

    # 4. Core lead fields available as placeholders
    context["reseller_id"] = lead.reseller_id
    context["lead_id"] = lead.id
    if lead.request_id:
        context["request_id"] = lead.request_id
    if lead.template:
        context["template_name"] = lead.template
    if lead.merchant_id:
        context["merchant_id"] = lead.merchant_id

    return context


def _apply_default(
    pre_check: PreCheckConfig,
    reason: str,
    response_data: Optional[Dict[str, Any]] = None,
) -> SinglePreCheckResult:
    """
    Apply the default action when a pre-check fails to get a definitive answer.
    """
    if pre_check.default_on_failure == PreCheckDefaultAction.PROCEED:
        logger.info(
            f"Pre-check '{pre_check.name}': defaulting to PROCEED (fail-open). Reason: {reason}"
        )
        return SinglePreCheckResult(
            name=pre_check.name,
            passed=True,
            reason=f"Default: proceed (fail-open). {reason}",
            response_data=response_data,
        )
    else:
        logger.info(
            f"Pre-check '{pre_check.name}': defaulting to SKIP (fail-closed). Reason: {reason}"
        )
        return SinglePreCheckResult(
            name=pre_check.name,
            passed=False,
            reason=f"Default: skip (fail-closed). {reason}",
            response_data=response_data,
        )


def _convert_auth_dict_to_config(
    auth_dict: Optional[Dict[str, Any]],
) -> Optional[HttpAuthConfig]:
    """
    Convert a raw auth dict from PreCheckHttpRequest to HttpAuthConfig.

    Expected dict structure:
    {
        "type": "bearer" | "basic" | "api_key" | "custom",
        "credential_id": "...",  # server-side credential reference
        "token": "...",           # for bearer
        "username": "...",        # for basic
        "password": "...",        # for basic
        "api_key_name": "...",    # for api_key
        "api_key_value": "...",   # for api_key
        "header_bindings": {"X-Client-Id": "client_id"}  # for custom
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
            "custom": HttpAuthType.CUSTOM,
        }

        auth_type = type_mapping.get(auth_type_str)
        if not auth_type:
            logger.warning(f"Unknown auth type '{auth_type_str}', skipping auth config")
            return None

        return HttpAuthConfig(
            type=auth_type,
            credential_id=auth_dict.get("credential_id"),
            token=auth_dict.get("token"),
            username=auth_dict.get("username"),
            password=auth_dict.get("password"),
            api_key_name=auth_dict.get("api_key_name"),
            api_key_value=auth_dict.get("api_key_value"),
            header_bindings=auth_dict.get("header_bindings") or {},
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


async def run_pre_checks(
    pre_checks: List[PreCheckConfig],
    lead: LeadCallTracker,
    template: Optional[TemplateModel],
    session: aiohttp.ClientSession,
) -> PreCheckResult:
    """
    Run all configured pre-checks for a lead.

    All enabled pre-checks must pass for the call to proceed (AND logic).
    Stops on first failure (fail-fast).

    Args:
        pre_checks: List of pre-check configurations from call_execution_config
        lead: The lead being processed
        template: The template for this lead (provides secrets for placeholder resolution)
        session: aiohttp session for making HTTP requests

    Returns:
        PreCheckResult with aggregated pass/fail and individual results
    """
    if not pre_checks:
        return PreCheckResult(should_proceed=True)

    results: List[SinglePreCheckResult] = []
    executor = HttpRequestExecutor(session)
    credential_cache: Dict[str, Credential] = {}

    for pre_check in pre_checks:
        if not pre_check.enabled:
            logger.info(f"Pre-check '{pre_check.name}': skipped (disabled)")
            results.append(
                SinglePreCheckResult(
                    name=pre_check.name,
                    passed=True,
                    reason="Skipped (disabled)",
                )
            )
            continue

        if pre_check.type != PreCheckType.EXTERNAL_API:
            logger.warning(
                f"Pre-check '{pre_check.name}': unknown type '{pre_check.type}', skipping"
            )
            results.append(
                SinglePreCheckResult(
                    name=pre_check.name,
                    passed=True,
                    reason=f"Unknown type '{pre_check.type}', skipped",
                )
            )
            continue

        # Build context with credential_id from this pre-check
        context = await _build_resolution_context(
            lead, template, pre_check.credential_id
        )

        # Execute HTTP request using HttpRequestExecutor
        try:
            # Convert PreCheckHttpRequest to HttpRequestConfig
            # This is inside try block to handle validation errors gracefully
            http_cfg = pre_check.http_request

            # Convert raw pre-check JSON, then resolve any credential reference.
            auth_config = _convert_auth_dict_to_config(http_cfg.auth)
            reseller_id, merchant_id = resolve_credential_scope(template, lead)
            resolved_auth = await resolve_credential_auth(
                auth_config,
                reseller_id=reseller_id,
                merchant_id=merchant_id,
                credential_cache=credential_cache,
            )

            # Convert query_params to Dict[str, str]
            converted_query_params = _convert_query_params_to_str_dict(
                http_cfg.query_params
            )

            http_config = HttpRequestConfig(
                url=http_cfg.url,
                method=HttpMethod(http_cfg.method.upper()),  # Normalize to uppercase
                headers=http_cfg.headers or {},
                body=http_cfg.body,
                auth=resolved_auth,
                query_params=converted_query_params,
                timeout=http_cfg.timeout,
                max_retries=http_cfg.max_retries,
            )

            logger.info(
                f"Pre-check '{pre_check.name}': executing {http_config.method.value} {http_cfg.url}"
            )

            response = await executor.execute(
                config=http_config,
                resolved_fields=context,
                fire_and_forget=False,  # We need the response
            )

            if response is None or response == (0, ""):
                result = _apply_default(pre_check, "HTTP request failed")
                results.append(result)
                if not result.passed:
                    return PreCheckResult(should_proceed=False, results=results)
                continue

            status_code, response_body = response

            # Check for non-success status
            if status_code < 200 or status_code >= 300:
                result = _apply_default(pre_check, f"API returned status {status_code}")
                results.append(result)
                if not result.passed:
                    return PreCheckResult(should_proceed=False, results=results)
                continue

            # Parse response JSON
            try:
                response_data = json.loads(response_body)
            except json.JSONDecodeError:
                logger.warning(
                    f"Pre-check '{pre_check.name}': response is not valid JSON"
                )
                result = _apply_default(pre_check, "Response is not valid JSON")
                results.append(result)
                if not result.passed:
                    return PreCheckResult(should_proceed=False, results=results)
                continue

            # Check response field
            response_config = pre_check.response_config
            check_field = response_config.response_field

            if check_field not in response_data:
                logger.warning(
                    f"Pre-check '{pre_check.name}': field '{check_field}' not found in response"
                )
                result = _apply_default(
                    pre_check,
                    f"Field '{check_field}' not found in response",
                    response_data,
                )
                results.append(result)
                if not result.passed:
                    return PreCheckResult(should_proceed=False, results=results)
                continue

            actual_value = response_data[check_field]
            expected_value = response_config.response_field_value
            match_type = response_config.match_type

            # Apply the configured operator (defaults to equals) to decide whether
            # the call should proceed. Supports equals/not_equals, list-aware
            # contains/not_contains (e.g. tags), and numeric gt/lt.
            should_proceed = _value_matches(actual_value, expected_value, match_type)
            logger.info(
                f"Pre-check '{pre_check.name}': {check_field}={actual_value}, "
                f"match_type={match_type.value}, expected={expected_value}, "
                f"should_proceed={should_proceed}"
            )

            result = SinglePreCheckResult(
                name=pre_check.name,
                passed=should_proceed,
                reason=f"{check_field}={actual_value}",
                response_data=response_data,
            )
            results.append(result)

            # Fail-fast: stop on first failure
            if not result.passed:
                logger.info(
                    f"Pre-check '{pre_check.name}' FAILED for lead {lead.id}: {result.reason}"
                )
                return PreCheckResult(should_proceed=False, results=results)

        except Exception as e:
            logger.error(
                f"Pre-check '{pre_check.name}': execution error: {e}", exc_info=True
            )
            result = _apply_default(pre_check, f"Execution error: {e}")
            results.append(result)
            if not result.passed:
                return PreCheckResult(should_proceed=False, results=results)

    logger.info(f"All pre-checks passed for lead {lead.id}")
    return PreCheckResult(should_proceed=True, results=results)
