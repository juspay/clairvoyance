"""
HTTP request executor for voice agent hooks and global functions.

Executes HTTP requests with:
- Multiple authentication methods (Bearer, Basic, API Key)
- Retry logic with exponential backoff
- Timeout enforcement
- Comprehensive error handling and logging
- Auto-detection of SSE streaming from response Content-Type

Supports two modes:
- fire_and_forget=True: For hooks (no response needed)
- fire_and_forget=False: For global functions (returns response to LLM)

When the response Content-Type is ``text/event-stream``, the executor reads
SSE events line-by-line. Each parsed event is forwarded via an optional
``on_sse_event`` callback (used for RTVI forwarding). Only the last event
is kept and returned to the caller — the LLM never sees intermediate events.
"""

import asyncio
import base64
import ipaddress
import json
from collections.abc import Awaitable
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

import aiohttp
from pydantic import SecretStr

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    replace_placeholders,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthConfig,
    HttpAuthType,
    HttpRequestConfig,
)
from app.core.config.static import (
    ENVIRONMENT,
    HTTP_REQUEST_BLOCKED_CONTENT_TYPES,
    HTTP_REQUEST_MAX_REDIRECTS,
    HTTP_REQUEST_MAX_RESPONSE_BYTES,
)
from app.core.logger import logger


class HttpRequestExecutor:
    """Executes HTTP requests with authentication, retry, and error handling"""

    def __init__(self, session: aiohttp.ClientSession):
        """
        Initialize executor with aiohttp session.

        Args:
            session: Shared aiohttp.ClientSession (with proxy support from context)
        """
        self.session = session

    async def execute(
        self,
        config: HttpRequestConfig,
        resolved_fields: Optional[dict] = None,
        fire_and_forget: bool = True,
        on_sse_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Optional[Tuple[int, str]]:
        """
        Execute HTTP request with template resolution.

        When the response Content-Type is ``text/event-stream``, the response
        is read line-by-line as SSE. Each parsed event is forwarded via the
        ``on_sse_event`` callback (if provided). Only the last event's data
        is returned to the caller — intermediate events are forwarded but
        discarded. This allows real-time RTVI forwarding while keeping the
        LLM payload small.

        SSE events must be terminated by a blank line. The upstream API
        should send a sentinel event (e.g. ``data: [DONE]``) to signal
        end-of-stream.

        Args:
            config: HttpRequestConfig with url, method, headers, body, auth, etc.
            resolved_fields: Dictionary of resolved field values to replace in templates.
                            Defaults to empty dict if None.
            fire_and_forget: If True (default), returns None (hook behavior).
                            If False, returns (status_code, response_body) tuple.
            on_sse_event: Optional async callback invoked for each parsed SSE event.
                          Must be a coroutine function. Receives the event dict
                          ({"event": ..., "data": ...}).

        Returns:
            None if fire_and_forget=True
            Tuple[int, str] (status_code, response_body) if fire_and_forget=False
            For SSE responses, response_body is the JSON of the last event's data.
            Returns (0, "") on failure when fire_and_forget=False

        Raises:
            Does not raise exceptions - logs errors instead
        """
        # Default to empty dict if None
        if resolved_fields is None:
            resolved_fields = {}
        try:
            # Apply template resolution to all parts of the request
            resolved_url = replace_placeholders(config.url, resolved_fields)
            resolved_headers_dict = self._resolve_dict_templates(
                config.headers, resolved_fields
            )
            resolved_query_params = self._resolve_dict_templates(
                config.query_params, resolved_fields
            )
            resolved_auth = self._resolve_auth_config(config.auth, resolved_fields)

            resolved_body = self._resolve_body(config.body, resolved_fields)

            # Build headers with authentication
            headers = self._build_headers_with_auth(
                resolved_headers_dict, resolved_auth, bool(resolved_body)
            )

            # Build full URL with query params
            url = self._build_url_with_params(resolved_url, resolved_query_params)

            # SSRF Protection: Validate the resolved URL before making the request
            self._validate_resolved_url(url)

            # Execute with retry
            for attempt in range(1, config.max_retries + 1):
                try:
                    logger.info(
                        f"HTTP {config.method.value} request to {url} (attempt {attempt}/{config.max_retries})"
                    )

                    async with self.session.request(
                        method=config.method.value,
                        url=url,
                        headers=headers,
                        json=resolved_body if resolved_body else None,
                        timeout=aiohttp.ClientTimeout(total=config.timeout),
                        max_redirects=HTTP_REQUEST_MAX_REDIRECTS,
                        allow_redirects=HTTP_REQUEST_MAX_REDIRECTS > 0,
                    ) as response:
                        status = response.status

                        # Security Check 1: Validate Content-Type header
                        content_type = response.headers.get("Content-Type", "").lower()
                        for blocked_type in HTTP_REQUEST_BLOCKED_CONTENT_TYPES:
                            if blocked_type in content_type:
                                error_msg = (
                                    f"Blocked content type '{content_type}' - "
                                    f"download of executables/scripts is not allowed"
                                )
                                logger.error(f"HTTP {config.method.value}: {error_msg}")
                                if fire_and_forget:
                                    return None
                                return (0, error_msg)

                        # SSE branch: stream events when the server opts in
                        # with text/event-stream and the caller supplied a
                        # per-event callback. Only on 2xx — error bodies fall
                        # through to the bulk read path below.
                        is_sse = "text/event-stream" in content_type
                        if (
                            is_sse
                            and on_sse_event is not None
                            and not fire_and_forget
                            and 200 <= status < 300
                        ):
                            concatenated = await self._consume_sse_stream(
                                response=response,
                                on_sse_event=on_sse_event,
                            )
                            logger.info(
                                f"HTTP {config.method.value} SSE stream complete: "
                                f"total_data_bytes={len(concatenated)}"
                            )
                            return (status, concatenated)

                        # Security Check 2: Validate Content-Length header
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                length = int(content_length)
                                if length > HTTP_REQUEST_MAX_RESPONSE_BYTES:
                                    error_msg = (
                                        f"Response too large: {length} bytes exceeds "
                                        f"max allowed {HTTP_REQUEST_MAX_RESPONSE_BYTES} bytes"
                                    )
                                    logger.error(
                                        f"HTTP {config.method.value}: {error_msg}"
                                    )
                                    if fire_and_forget:
                                        return None
                                    return (0, error_msg)
                            except ValueError:
                                pass  # Invalid Content-Length, will check actual size below

                        # Read response with size limit
                        response_bytes = await response.content.read(
                            HTTP_REQUEST_MAX_RESPONSE_BYTES + 1
                        )
                        if len(response_bytes) > HTTP_REQUEST_MAX_RESPONSE_BYTES:
                            error_msg = (
                                f"Response exceeded max size of "
                                f"{HTTP_REQUEST_MAX_RESPONSE_BYTES} bytes"
                            )
                            logger.error(f"HTTP {config.method.value}: {error_msg}")
                            if fire_and_forget:
                                return None
                            return (0, error_msg)

                        # Decode response text
                        response_text = response_bytes.decode("utf-8", errors="replace")

                        logger.info(
                            f"HTTP {config.method.value} response: status={status}, "
                            f"body_preview={response_text[:200]}"
                        )

                        # Success
                        if 200 <= status < 300:
                            logger.info(f"HTTP {config.method.value} request succeeded")
                            if fire_and_forget:
                                return None
                            return (status, response_text)

                        # Non-success status code
                        logger.warning(
                            f"HTTP {config.method.value} returned non-success status {status}: {response_text[:500]}"
                        )

                        # Don't retry on 4xx client errors (except 429 rate limit)
                        if 400 <= status < 500 and status != 429:
                            logger.error(
                                f"HTTP {config.method.value} client error {status}, not retrying"
                            )
                            if fire_and_forget:
                                return None
                            return (status, response_text)

                except asyncio.TimeoutError:
                    logger.warning(
                        f"HTTP {config.method.value} timeout after {config.timeout}s (attempt {attempt})"
                    )
                except aiohttp.TooManyRedirects:
                    logger.error(
                        f"HTTP {config.method.value} exceeded max redirects "
                        f"({HTTP_REQUEST_MAX_REDIRECTS}), not retrying"
                    )
                    if fire_and_forget:
                        return None
                    return (
                        0,
                        f"Too many redirects (max: {HTTP_REQUEST_MAX_REDIRECTS})",
                    )
                except aiohttp.ClientError as e:
                    logger.warning(
                        f"HTTP {config.method.value} client error: {e} (attempt {attempt})"
                    )
                except Exception as e:
                    logger.error(
                        f"HTTP {config.method.value} unexpected error: {e} (attempt {attempt})",
                        exc_info=True,
                    )

                # Exponential backoff before retry
                if attempt < config.max_retries:
                    backoff_time = 2 ** (attempt - 1)  # 1s, 2s, 4s, 8s...
                    logger.info(f"Retrying in {backoff_time}s...")
                    await asyncio.sleep(backoff_time)

            # All retries exhausted
            logger.error(
                f"HTTP {config.method.value} to {url} failed after {config.max_retries} attempts"
            )
            if fire_and_forget:
                return None
            return (0, "")

        except Exception as e:
            logger.error(
                f"HTTP request execution failed: {e}",
                exc_info=True,
            )
            if fire_and_forget:
                return None
            return (0, str(e))

    @staticmethod
    async def _consume_sse_stream(
        response: aiohttp.ClientResponse,
        on_sse_event: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> str:
        """Parse a text/event-stream response line by line.

        SSE: blank line terminates an event; multiple ``data:`` lines join with
        ``\\n``; lines starting with ``:`` are comments; fields have the form
        ``field: value`` with an optional space after the colon. Stops once
        emitted data exceeds ``HTTP_REQUEST_MAX_RESPONSE_BYTES``.
        """
        chunks: list[str] = []
        total_bytes = 0
        event_name: Optional[str] = None
        event_id: Optional[str] = None
        data_lines: list[str] = []

        async def dispatch() -> None:
            nonlocal event_name, event_id, data_lines, total_bytes
            if not data_lines and event_name is None:
                return
            event_data = "\n".join(data_lines)
            if event_data.strip() == "[DONE]":
                event_name = None
                event_id = None
                data_lines = []
                return
            try:
                await on_sse_event(
                    {"event": event_name, "id": event_id, "data": event_data}
                )
            except Exception as cb_err:
                logger.warning(f"on_sse_event callback raised: {cb_err}")
            chunks.append(event_data)
            total_bytes += len(event_data.encode("utf-8"))
            event_name = None
            event_id = None
            data_lines = []

        while True:
            line_bytes = await response.content.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                await dispatch()
                if total_bytes > HTTP_REQUEST_MAX_RESPONSE_BYTES:
                    logger.warning(
                        f"SSE stream exceeded {HTTP_REQUEST_MAX_RESPONSE_BYTES} bytes; closing"
                    )
                    break
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
            elif field == "id":
                event_id = value

        await dispatch()
        return "\n".join(chunks)

    def _build_headers_with_auth(
        self, headers_dict: dict, auth_config, has_body: bool
    ) -> dict:
        """
        Build headers with authentication (after template resolution).

        Args:
            headers_dict: Already resolved headers dictionary
            auth_config: Already resolved HttpAuthConfig or None
            has_body: Whether request has a body

        Returns:
            Dictionary of headers including auth headers
        """
        headers = headers_dict.copy()

        # Add authentication headers
        if auth_config:
            auth_headers = self._build_auth_headers_from_config(auth_config)
            headers.update(auth_headers)

        # Set default Content-Type for requests with body
        if has_body and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        return headers

    def _build_auth_headers_from_config(self, auth_config) -> dict:
        """
        Build authentication headers based on auth type (after template resolution).

        Args:
            auth_config: Resolved HttpAuthConfig

        Returns:
            Dictionary of authentication headers
        """
        headers = {}

        if not auth_config:
            return headers

        if auth_config.type == HttpAuthType.BEARER:
            if auth_config.token:
                headers["Authorization"] = (
                    f"Bearer {auth_config.token.get_secret_value()}"
                )
            else:
                logger.warning("Bearer auth configured but token is missing")

        elif auth_config.type == HttpAuthType.BASIC:
            if auth_config.username and auth_config.password:
                credentials = (
                    f"{auth_config.username}:{auth_config.password.get_secret_value()}"
                )
                encoded = base64.b64encode(credentials.encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            else:
                logger.warning("Basic auth configured but username/password missing")

        elif auth_config.type == HttpAuthType.API_KEY:
            if auth_config.api_key_name and auth_config.api_key_value:
                headers[auth_config.api_key_name] = (
                    auth_config.api_key_value.get_secret_value()
                )
            else:
                logger.warning("API Key auth configured but name/value missing")

        elif auth_config.type == HttpAuthType.WISMO:
            from app.core.config.static import ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY

            if ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY:
                headers["Authorization"] = ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
            else:
                logger.warning(
                    "WISMO auth configured but ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY is not set"
                )

        return headers

    @staticmethod
    def _build_url_with_params(url: str, params: dict) -> str:
        """
        Build URL with query parameters.

        Uses urllib.parse.urlencode for proper URL encoding of special characters.

        Args:
            url: Base URL
            params: Query parameters (values can be str, int, list, or None)

        Returns:
            URL with properly encoded query string
        """
        if not params:
            return url

        # Filter out None values and convert non-string values
        clean_params: dict[str, Any] = {}
        for k, v in params.items():
            if v is None:
                continue
            clean_params[k] = v

        if not clean_params:
            return url

        # urlencode handles special characters and supports lists via doseq
        query_string = urlencode(clean_params, doseq=True)
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{query_string}"

    @staticmethod
    def _validate_resolved_url(url: str) -> None:
        """
        Validate resolved URL to prevent SSRF attacks.

        This runs AFTER template variable substitution, validating the actual URL
        that will be used for the HTTP request.

        Security checks:
        - HTTPS only (no HTTP)
        - Block localhost in production
        - Block private IP ranges (RFC1918, loopback, link-local)

        Args:
            url: Fully resolved URL (no template variables)

        Raises:
            ValueError: If URL fails security validation
        """
        # Parse URL
        try:
            parsed = urlparse(url)
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")

        # Check 1: HTTPS only
        if parsed.scheme != "https":
            raise ValueError(
                f"Only HTTPS URLs are allowed for security. Got scheme: {parsed.scheme}"
            )

        # Check 2: Must have hostname
        if not parsed.hostname:
            raise ValueError("URL must have a valid hostname")

        hostname = parsed.hostname.lower()

        # Check 3: Block localhost (production only)
        is_production = ENVIRONMENT.lower() in ("production", "prod")
        if is_production:
            localhost_patterns = ["localhost", "127.0.0.1", "::1", "0.0.0.0"]
            if hostname in localhost_patterns:
                raise ValueError(
                    f"Requests to localhost are not allowed in production: {hostname}"
                )

        # Check 4: Block private/reserved IP addresses
        # Try to parse as IP address
        try:
            ip = ipaddress.ip_address(hostname)

            # Block loopback (127.0.0.0/8, ::1)
            if ip.is_loopback:
                raise ValueError(
                    f"Requests to loopback addresses are not allowed: {ip}"
                )

            # Block private IPs (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
            if ip.is_private:
                raise ValueError(
                    f"Requests to private IP addresses are not allowed: {ip}"
                )

            # Block link-local (169.254.0.0/16, fe80::/10)
            if ip.is_link_local:
                raise ValueError(
                    f"Requests to link-local addresses are not allowed: {ip}"
                )

            # Block all non-global IPs (comprehensive check)
            if not ip.is_global:
                raise ValueError(
                    f"Requests to non-global IP addresses are not allowed: {ip}"
                )

        except ValueError as ip_error:
            # If it's already a validation error we raised, re-raise it
            if "not allowed" in str(ip_error):
                raise
            # Otherwise, hostname is not an IP - it's a domain name, which is allowed

    def _resolve_template_json_safe(
        self, template_str: str, resolved_fields: dict
    ) -> str:
        """
        Resolve placeholders in a template string with JSON-escaped values.

        This is specifically for resolving placeholders in JSON body strings,
        where values may contain characters that would break JSON syntax
        (quotes, backslashes, newlines, etc.).

        Args:
            template_str: String with {placeholder} templates
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            String with placeholders replaced by JSON-escaped values

        Example:
            template_str = '{"name": "{customer_name}"}'
            resolved_fields = {"customer_name": 'John "Jack" Doe'}
            returns: '{"name": "John \\"Jack\\" Doe"}'
        """
        if not isinstance(template_str, str):
            return template_str

        result = template_str
        for field_name, value in resolved_fields.items():
            placeholder = f"{{{field_name}}}"
            if placeholder in result:
                # JSON-escape the value to handle special characters
                # json.dumps adds quotes around the string, so we strip them
                if isinstance(value, str):
                    escaped_value = json.dumps(value)[1:-1]  # Remove surrounding quotes
                else:
                    # For non-strings (int, float, bool, None), convert to JSON representation
                    escaped_value = json.dumps(value)
                result = result.replace(placeholder, escaped_value)
                logger.debug(
                    f"Replaced placeholder '{placeholder}' with JSON-escaped value in template"
                )

        return result

    def _resolve_dict_templates(
        self, template_dict: dict[str, Any], resolved_fields: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Recursively resolve templates in all string values of a dictionary.

        Args:
            template_dict: Dictionary with potentially templated string values
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            Dictionary with all template strings resolved
        """
        if not template_dict:
            return {}

        result: dict[str, Any] = {}
        for key, value in template_dict.items():
            if isinstance(value, str):
                result[key] = replace_placeholders(value, resolved_fields)
            elif isinstance(value, dict):
                result[key] = self._resolve_dict_templates(value, resolved_fields)
            else:
                result[key] = value

        return result

    def _resolve_recursive(self, obj, resolved_fields: dict):
        """
        Recursively resolve templates in nested structures (dicts, lists, strings).

        Args:
            obj: Object to resolve (dict, list, str, or primitive)
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            Resolved object with all templates replaced
        """
        if obj is None:
            return None
        elif isinstance(obj, dict):
            return {
                key: self._resolve_recursive(value, resolved_fields)
                for key, value in obj.items()
            }
        elif isinstance(obj, list):
            return [self._resolve_recursive(item, resolved_fields) for item in obj]
        elif isinstance(obj, str):
            return replace_placeholders(obj, resolved_fields)
        else:
            # Primitive types (int, float, bool) return as-is
            return obj

    def _resolve_body(
        self, body: Optional[dict | list | str], resolved_fields: dict
    ) -> Optional[dict | list]:
        """
        Resolve HTTP request body, handling dict, list, and JSON string formats.

        Args:
            body: Request body as dict, list, or JSON string (may contain {placeholders})
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            Resolved body as dict or list (ready for json= parameter) or None

        Raises:
            ValueError: If body is a string that cannot be parsed as valid JSON

        Examples:
            # Dict body (standard)
            body = {"order_id": "{order_id}", "status": "confirmed"}
            returns: {"order_id": "ORD-123", "status": "confirmed"}

            # String body (from frontend)
            body = '{"order_id": "{order_id}", "status": "confirmed"}'
            returns: {"order_id": "ORD-123", "status": "confirmed"}
        """
        if body is None:
            return None

        # If body is already a dict or list, resolve placeholders recursively
        if isinstance(body, (dict, list)):
            return self._resolve_recursive(body, resolved_fields)

        # If body is a string, first resolve placeholders, then parse as JSON
        if isinstance(body, str):
            # Step 1: Resolve {placeholder} templates in the string
            # Use JSON-safe resolution to escape special characters in values
            resolved_string = self._resolve_template_json_safe(body, resolved_fields)

            # Step 2: Parse the resolved string as JSON
            try:
                parsed_body = json.loads(resolved_string)
                if not isinstance(parsed_body, (dict, list)):
                    logger.warning(
                        f"Parsed body is not a dict or list (got {type(parsed_body).__name__}), "
                        f"wrapping in 'data' key"
                    )
                    return {"data": parsed_body}
                return parsed_body
            except json.JSONDecodeError as e:
                logger.error(
                    f"Failed to parse body as JSON: {e}. "
                    f"Body preview: {resolved_string[:200]}..."
                )
                raise ValueError(
                    f"Invalid JSON in request body: {e}. "
                    f"Please ensure the body is valid JSON with proper syntax "
                    f"(check for missing commas, quotes, etc.)"
                )

        # Unexpected type
        logger.warning(f"Unexpected body type: {type(body).__name__}, returning None")
        return None

    def _resolve_auth_config(self, auth_config, resolved_fields: dict):
        """
        Resolve templates in authentication configuration.

        Args:
            auth_config: HttpAuthConfig or None
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            New HttpAuthConfig with resolved values or None
        """
        if not auth_config:
            return None

        # Create new auth config with resolved values
        # For SecretStr fields, extract value, resolve template, then wrap back in SecretStr
        resolved_token = None
        if auth_config.token:
            resolved_token_str = replace_placeholders(
                auth_config.token.get_secret_value(), resolved_fields
            )
            resolved_token = SecretStr(resolved_token_str)

        resolved_password = None
        if auth_config.password:
            resolved_password_str = replace_placeholders(
                auth_config.password.get_secret_value(), resolved_fields
            )
            resolved_password = SecretStr(resolved_password_str)

        resolved_api_key_value = None
        if auth_config.api_key_value:
            resolved_api_key_value_str = replace_placeholders(
                auth_config.api_key_value.get_secret_value(), resolved_fields
            )
            resolved_api_key_value = SecretStr(resolved_api_key_value_str)

        return HttpAuthConfig(
            type=auth_config.type,
            token=resolved_token,
            username=(
                replace_placeholders(auth_config.username, resolved_fields)
                if auth_config.username
                else None
            ),
            password=resolved_password,
            api_key_name=(
                replace_placeholders(auth_config.api_key_name, resolved_fields)
                if auth_config.api_key_name
                else None
            ),
            api_key_value=resolved_api_key_value,
        )
