"""
HTTP request executor for voice agent hooks.

Executes HTTP requests with:
- Multiple authentication methods (Bearer, Basic, API Key)
- Retry logic with exponential backoff
- Timeout enforcement
- Comprehensive error handling and logging

Supports fire-and-forget pattern for hooks (no response needed).
"""

import asyncio
import base64

import aiohttp

from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthType,
    HttpRequestConfig,
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
        resolved_fields: dict,
    ) -> None:
        """
        Execute HTTP request with template resolution (fire-and-forget).

        Args:
            config: HttpRequestConfig with url, method, headers, body, auth, etc.
            resolved_fields: Dictionary of resolved field values to replace in templates

        Raises:
            Does not raise exceptions - logs errors instead (fire-and-forget pattern)
        """
        try:
            # Apply template resolution to all parts of the request
            resolved_url = self._resolve_template(config.url, resolved_fields)
            resolved_headers_dict = self._resolve_dict_templates(
                config.headers, resolved_fields
            )
            resolved_query_params = self._resolve_dict_templates(
                config.query_params, resolved_fields
            )
            resolved_body = self._resolve_recursive(config.body, resolved_fields)
            resolved_auth = self._resolve_auth_config(config.auth, resolved_fields)

            # Build headers with authentication
            headers = self._build_headers_with_auth(
                resolved_headers_dict, resolved_auth, resolved_body
            )

            # Build full URL with query params
            url = self._build_url_with_params(resolved_url, resolved_query_params)

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
                    ) as response:
                        status = response.status
                        response_text = await response.text()

                        logger.info(
                            f"HTTP {config.method.value} response: status={status}, "
                            f"body_preview={response_text[:200]}"
                        )

                        # Success
                        if 200 <= status < 300:
                            logger.info(f"HTTP {config.method.value} request succeeded")
                            return

                        # Non-success status code
                        logger.warning(
                            f"HTTP {config.method.value} returned non-success status {status}: {response_text[:500]}"
                        )

                        # Don't retry on 4xx client errors (except 429 rate limit)
                        if 400 <= status < 500 and status != 429:
                            logger.error(
                                f"HTTP {config.method.value} client error {status}, not retrying"
                            )
                            return

                except asyncio.TimeoutError:
                    logger.warning(
                        f"HTTP {config.method.value} timeout after {config.timeout}s (attempt {attempt})"
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

        except Exception as e:
            logger.error(
                f"HTTP request execution failed: {e}",
                exc_info=True,
            )

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
                headers["Authorization"] = f"Bearer {auth_config.token}"
            else:
                logger.warning("Bearer auth configured but token is missing")

        elif auth_config.type == HttpAuthType.BASIC:
            if auth_config.username and auth_config.password:
                credentials = f"{auth_config.username}:{auth_config.password}"
                encoded = base64.b64encode(credentials.encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            else:
                logger.warning("Basic auth configured but username/password missing")

        elif auth_config.type == HttpAuthType.API_KEY:
            if auth_config.api_key_name and auth_config.api_key_value:
                headers[auth_config.api_key_name] = auth_config.api_key_value
            else:
                logger.warning("API Key auth configured but name/value missing")

        return headers

    @staticmethod
    def _build_url_with_params(url: str, params: dict) -> str:
        """
        Build URL with query parameters.

        Args:
            url: Base URL
            params: Query parameters

        Returns:
            URL with query string
        """
        if not params:
            return url

        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{query_string}"

    def _resolve_template(self, template_str: str, resolved_fields: dict) -> str:
        """
        Replace {field_name} placeholders in a string with resolved values.

        Args:
            template_str: String that may contain {field_name} placeholders
            resolved_fields: Dictionary of field_name -> value mappings

        Returns:
            String with all placeholders replaced

        Example:
            template_str = "Order {order_id} for {customer_name}"
            resolved_fields = {"order_id": "123", "customer_name": "John"}
            returns: "Order 123 for John"
        """
        if not isinstance(template_str, str):
            return template_str

        result = template_str
        for field_name, value in resolved_fields.items():
            placeholder = f"{{{field_name}}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value))
                logger.debug(
                    f"Replaced placeholder '{placeholder}' with '{value}' in template"
                )

        return result

    def _resolve_dict_templates(
        self, template_dict: dict, resolved_fields: dict
    ) -> dict:
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

        result = {}
        for key, value in template_dict.items():
            if isinstance(value, str):
                result[key] = self._resolve_template(value, resolved_fields)
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
            return self._resolve_template(obj, resolved_fields)
        else:
            # Primitive types (int, float, bool) return as-is
            return obj

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

        # Import here to avoid circular dependency
        from app.ai.voice.agents.breeze_buddy.template.types import HttpAuthConfig

        # Create new auth config with resolved values
        return HttpAuthConfig(
            type=auth_config.type,
            token=(
                self._resolve_template(auth_config.token, resolved_fields)
                if auth_config.token
                else None
            ),
            username=(
                self._resolve_template(auth_config.username, resolved_fields)
                if auth_config.username
                else None
            ),
            password=(
                self._resolve_template(auth_config.password, resolved_fields)
                if auth_config.password
                else None
            ),
            api_key_name=(
                self._resolve_template(auth_config.api_key_name, resolved_fields)
                if auth_config.api_key_name
                else None
            ),
            api_key_value=(
                self._resolve_template(auth_config.api_key_value, resolved_fields)
                if auth_config.api_key_value
                else None
            ),
        )
