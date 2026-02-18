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

from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthConfig,
    HttpAuthType,
    HttpMethod,
    HttpRequestConfig,
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_as_template_vars,
)
from app.schemas import (
    LeadCallTracker,
    PreCheckConfig,
    PreCheckDefaultAction,
    PreCheckType,
)


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
) -> Dict[str, Any]:
    """
    Build the placeholder resolution context by merging credentials, template secrets, and payload.

    Resolution order (later overrides earlier):
    1. Credentials from credentials table (global + merchant-specific)
    2. Template secrets (API keys, tokens, base URLs)
    3. Lead payload (customer data, order data)
    4. Core lead fields (merchant_id, lead_id, etc.)
    """
    context: Dict[str, Any] = {}

    # 1. Credentials from credentials table
    try:
        credential_vars = await get_credentials_as_template_vars(lead.merchant_id)
        if credential_vars:
            context.update(credential_vars)
    except Exception as e:
        logger.warning(f"Failed to load credentials for pre-check context: {e}")

    # 2. Template secrets (override credentials for same keys)
    if template and template.secrets:
        context.update(template.secrets)

    # 3. Lead payload (override both)
    if lead.payload:
        context.update(lead.payload)

    # 4. Core lead fields available as placeholders
    context["merchant_id"] = lead.merchant_id
    context["lead_id"] = lead.id
    if lead.request_id:
        context["request_id"] = lead.request_id
    if lead.template:
        context["template_name"] = lead.template
    if lead.shop_identifier:
        context["shop_identifier"] = lead.shop_identifier

    return context


def _resolve_placeholders(value: Any, context: Dict[str, Any]) -> Any:
    """
    Recursively resolve {placeholder} patterns in strings, dicts, and lists.
    """
    if isinstance(value, str):
        result = value
        for key, val in context.items():
            placeholder = f"{{{key}}}"
            if placeholder in result:
                # If the entire string is a single placeholder, preserve the original type
                if result == placeholder:
                    return val
                result = result.replace(placeholder, str(val))
        return result
    elif isinstance(value, dict):
        return {k: _resolve_placeholders(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_placeholders(item, context) for item in value]
    return value


def _build_http_request_config(
    pre_check: PreCheckConfig,
    context: Dict[str, Any],
) -> HttpRequestConfig:
    """
    Build an HttpRequestConfig from the pre-check config with resolved placeholders.
    """
    http_cfg = pre_check.http_request

    resolved_url = _resolve_placeholders(http_cfg.url, context)
    resolved_headers = _resolve_placeholders(http_cfg.headers or {}, context)
    resolved_body = (
        _resolve_placeholders(http_cfg.body, context) if http_cfg.body else None
    )

    # Build auth config if provided
    auth_config = None
    if http_cfg.auth:
        resolved_auth = _resolve_placeholders(http_cfg.auth, context)
        auth_type = resolved_auth.get("type", "none")
        auth_config = HttpAuthConfig(
            type=HttpAuthType(auth_type),
            token=resolved_auth.get("token"),
            username=resolved_auth.get("username"),
            password=resolved_auth.get("password"),
            api_key_name=resolved_auth.get("api_key_name"),
            api_key_value=resolved_auth.get("api_key_value"),
        )

    return HttpRequestConfig(
        url=resolved_url,
        method=HttpMethod(http_cfg.method),
        headers=resolved_headers,
        body=resolved_body,
        auth=auth_config,
        timeout=http_cfg.timeout,
        max_retries=http_cfg.max_retries,
    )


async def _execute_external_api_pre_check(
    pre_check: PreCheckConfig,
    context: Dict[str, Any],
    session: aiohttp.ClientSession,
) -> SinglePreCheckResult:
    """
    Execute a single external API pre-check.

    Makes an HTTP request to the configured endpoint and interprets the response
    based on the response_config to determine if the call should proceed.
    """
    name = pre_check.name

    try:
        http_config = _build_http_request_config(pre_check, context)
        response_config = pre_check.response_config

        # Build headers
        headers = dict(http_config.headers) if http_config.headers else {}
        if http_config.body and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        # Build auth headers
        if http_config.auth:
            if http_config.auth.type == HttpAuthType.BEARER and http_config.auth.token:
                token = http_config.auth.token
                token_str = (
                    token.get_secret_value()
                    if hasattr(token, "get_secret_value")
                    else str(token)
                )
                headers["Authorization"] = f"Bearer {token_str}"
            elif http_config.auth.type == HttpAuthType.API_KEY:
                if http_config.auth.api_key_name and http_config.auth.api_key_value:
                    key_val = http_config.auth.api_key_value
                    key_str = (
                        key_val.get_secret_value()
                        if hasattr(key_val, "get_secret_value")
                        else str(key_val)
                    )
                    headers[http_config.auth.api_key_name] = key_str

        # Prepare body
        body_json = None
        if http_config.body:
            body_json = http_config.body if isinstance(http_config.body, dict) else None

        logger.info(
            f"Pre-check '{name}': executing {http_config.method.value} {http_config.url}"
        )

        # Execute with retry
        last_error = None
        for attempt in range(1, http_config.max_retries + 1):
            try:
                async with session.request(
                    method=http_config.method.value,
                    url=http_config.url,
                    headers=headers,
                    json=body_json,
                    timeout=aiohttp.ClientTimeout(total=http_config.timeout),
                ) as response:
                    status_code = response.status
                    response_text = await response.text()

                    logger.info(
                        f"Pre-check '{name}': response status={status_code}, "
                        f"body_preview={response_text[:200]}"
                    )

                    if 200 <= status_code < 300:
                        # Parse response
                        try:
                            response_data = json.loads(response_text)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"Pre-check '{name}': response is not valid JSON"
                            )
                            return _apply_default(
                                pre_check, "Response is not valid JSON"
                            )

                        # Extract the should_proceed field
                        should_proceed_field = response_config.should_proceed_field
                        if should_proceed_field not in response_data:
                            logger.warning(
                                f"Pre-check '{name}': field '{should_proceed_field}' not found in response"
                            )
                            return _apply_default(
                                pre_check,
                                f"Field '{should_proceed_field}' not found in response",
                                response_data,
                            )

                        should_proceed = bool(response_data[should_proceed_field])

                        # Extract optional reason
                        reason = "No reason provided"
                        if (
                            response_config.reason_field
                            and response_config.reason_field in response_data
                        ):
                            reason = str(response_data[response_config.reason_field])

                        return SinglePreCheckResult(
                            name=name,
                            passed=should_proceed,
                            reason=reason,
                            response_data=response_data,
                        )
                    else:
                        # Non-success status - don't retry on 4xx
                        if 400 <= status_code < 500 and status_code != 429:
                            logger.warning(
                                f"Pre-check '{name}': client error {status_code}, not retrying"
                            )
                            return _apply_default(
                                pre_check,
                                f"API returned status {status_code}",
                            )
                        last_error = f"API returned status {status_code}"

            except aiohttp.ClientError as e:
                last_error = f"HTTP client error: {e}"
                logger.warning(
                    f"Pre-check '{name}': {last_error} (attempt {attempt}/{http_config.max_retries})"
                )
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                logger.warning(
                    f"Pre-check '{name}': {last_error} (attempt {attempt}/{http_config.max_retries})"
                )

        # All retries exhausted
        logger.warning(
            f"Pre-check '{name}': all {http_config.max_retries} attempts failed. Last error: {last_error}"
        )
        return _apply_default(pre_check, f"All attempts failed: {last_error}")

    except Exception as e:
        logger.error(f"Pre-check '{name}': execution error: {e}", exc_info=True)
        return _apply_default(pre_check, f"Execution error: {e}")


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

    context = await _build_resolution_context(lead, template)
    results: List[SinglePreCheckResult] = []

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

        if pre_check.type == PreCheckType.EXTERNAL_API:
            result = await _execute_external_api_pre_check(pre_check, context, session)
        else:
            logger.warning(
                f"Pre-check '{pre_check.name}': unknown type '{pre_check.type}', skipping"
            )
            result = SinglePreCheckResult(
                name=pre_check.name,
                passed=True,
                reason=f"Unknown type '{pre_check.type}', skipped",
            )

        results.append(result)

        # Fail-fast: stop on first failure
        if not result.passed:
            logger.info(
                f"Pre-check '{pre_check.name}' FAILED for lead {lead.id}: {result.reason}"
            )
            return PreCheckResult(should_proceed=False, results=results)

    logger.info(f"All pre-checks passed for lead {lead.id}")
    return PreCheckResult(should_proceed=True, results=results)
