"""OpenRouter — one door to every chat model it routes.

OpenRouter speaks the OpenAI chat-completions shape for every model behind
it (openai/*, anthropic/*, google/*, ...), so one class serves them all;
the request's ``model`` is the OpenRouter model id.

A request's ``schema`` becomes OpenRouter's strict structured output
(``response_format`` json_schema) with ``provider.require_parameters`` —
routed only to an endpoint that supports every parameter sent, so a model
without that support (or without a sent knob like ``temperature``) fails
loudly (404) instead of ignoring the shape. No schema = none of this: a
plain chat call. Strict mode accepts a subset of JSON Schema: bounds and
similar keywords are dropped here, so a caller that relies on them checks
them on ``structured``.
"""

import asyncio
import json
import random
import re
from typing import (
    Any,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Optional,
    TypedDict,
)

import httpx

from app.core.logger import logger
from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    GenerationSettings,
    JsonSchema,
    ProviderConfig,
    ProviderError,
    Usage,
)

# a reply wrapped in a markdown fence (```json ... ```), anywhere in the text
_FENCED = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)


class _WireMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class _WireJsonSchema(TypedDict):
    name: str
    strict: bool
    schema: Dict[str, object]


class _WireResponseFormat(TypedDict):
    type: Literal["json_schema"]
    json_schema: _WireJsonSchema


class OpenRouterProvider:
    name = "openrouter"

    _BACKOFF_BASE_SECONDS: ClassVar[float] = 2.0
    # the JSON Schema keywords every strict structured-outputs endpoint accepts
    _STRICT_KEYWORDS: ClassVar[FrozenSet[str]] = frozenset(
        {"type", "properties", "required", "enum", "description", "items"}
    )
    # the structured-outputs schema name (^[a-zA-Z0-9_-]+$)
    _SCHEMA_NAME: ClassVar[str] = "response"

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self._config.timeout_seconds),
                        limits=httpx.Limits(
                            max_connections=50, max_keepalive_connections=5
                        ),
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # --- request: PURE ----------------------------------------------------------

    @staticmethod
    def _settings(settings: GenerationSettings) -> Dict[str, object]:
        wire: Dict[str, object] = {
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "top_p": settings.top_p,
            "stop": list(settings.stop) if settings.stop is not None else None,
        }
        return {key: value for key, value in wire.items() if value is not None}

    @classmethod
    def _strict_schema(cls, schema: JsonSchema) -> Dict[str, object]:
        """The schema in OpenRouter's strict form: only the keywords every
        endpoint accepts, every object closed with all its properties
        required. Applied at every depth."""
        strict: Dict[str, object] = {
            key: value for key, value in schema.items() if key in cls._STRICT_KEYWORDS
        }
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            strict["properties"] = {
                name: cls._strict_schema(sub) if isinstance(sub, Mapping) else sub
                for name, sub in properties.items()
            }
            strict["required"] = list(properties)
            strict["additionalProperties"] = False
        items = schema.get("items")
        if isinstance(items, Mapping):
            strict["items"] = cls._strict_schema(items)
        return strict

    def body(self, request: GenerateRequest) -> Dict[str, object]:
        """The chat-completions body. ``extra`` goes in first, so it never
        overrides what the request sets; streaming is not supported. A chat
        model takes a conversation: a mapping ``input`` raises."""
        if isinstance(request.input, Mapping):
            raise self._error("input must be a sequence of messages, not a mapping")
        messages: List[_WireMessage] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.extend(
            {"role": message.role, "content": message.content}
            for message in request.input
        )
        body: Dict[str, object] = {
            **(request.extra or {}),
            **self._settings(request.settings),
            "model": request.model,
            "messages": messages,
            "stream": False,
        }
        if request.schema is not None:
            response_format: _WireResponseFormat = {
                "type": "json_schema",
                "json_schema": {
                    "name": self._SCHEMA_NAME,
                    "strict": True,
                    "schema": self._strict_schema(request.schema),
                },
            }
            body["response_format"] = response_format
            preferences = body.get("provider")
            body["provider"] = {
                **(preferences if isinstance(preferences, Mapping) else {}),
                "require_parameters": True,
            }
        return body

    # --- response: PURE ---------------------------------------------------------

    @staticmethod
    def _retryable(status: int) -> bool:
        # 429 and every 5xx; nothing else is fixed by asking again
        return status == 429 or 500 <= status < 600

    def _error(self, message: str, status: Optional[int] = None) -> ProviderError:
        return ProviderError(
            self.name,
            message,
            status=status,
            retryable=status is not None and self._retryable(status),
        )

    @staticmethod
    def _unfenced(text: str) -> str:
        match = _FENCED.search(text)
        return (match.group(1) if match else text).strip()

    def response(
        self, parsed: Mapping[str, Any], request: GenerateRequest
    ) -> GenerateResponse:
        """The vendor's reply as a ``GenerateResponse``. OpenRouter reports an
        upstream failure inside a 200 body too (top level or on the choice);
        that raises like an HTTP error with the same code."""
        choices = parsed.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        error = parsed.get("error")
        if error is None and isinstance(first, dict):
            error = first.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            detail = error.get("message") if isinstance(error, dict) else error
            if isinstance(code, str) and code.isdigit():  # e.g. "502"
                code = int(code)
            status = code if isinstance(code, int) else None
            # an upstream failure reported inside a 200: retried by its code;
            # with no code it is treated as transient
            raise ProviderError(
                self.name,
                f"vendor error {code}: {detail}",
                status=status,
                retryable=status is None or self._retryable(status),
            )
        if not isinstance(first, dict):
            raise self._error(f"no choices in reply: {str(parsed)[:300]}")

        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):  # multi-part content: keep the text parts
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        finish_reason = first.get("finish_reason")
        if content is None and finish_reason == "length":
            content = ""
        if not isinstance(content, str):
            raise self._error(f"no content in reply: {str(first)[:300]}")

        structured: Optional[object] = None
        if request.schema is not None:
            if finish_reason == "length":
                raise self._error("reply truncated (finish_reason=length)")
            try:
                structured = json.loads(self._unfenced(content))
            except json.JSONDecodeError as exc:
                raise self._error(f"reply is not JSON: {content[:300]}") from exc

        usage = parsed.get("usage")
        return GenerateResponse(
            content=content,
            model=str(parsed.get("model") or request.model),
            structured=structured,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            usage=(
                Usage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                )
                if isinstance(usage, Mapping)
                else None
            ),
        )

    # --- the call ---------------------------------------------------------------

    async def _attempt(
        self,
        client: httpx.AsyncClient,
        body: Mapping[str, object],
        request: GenerateRequest,
    ) -> GenerateResponse:
        try:
            reply = await client.post(
                self._config.base_url,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                f"transport error: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        if reply.status_code != 200:
            raise self._error(
                f"HTTP {reply.status_code}: {reply.text[:300]}", reply.status_code
            )
        try:
            parsed = reply.json()
        except ValueError as exc:
            raise self._error(f"reply is not JSON: {reply.text[:300]}") from exc
        if not isinstance(parsed, dict):
            raise self._error(f"reply is not a JSON object: {reply.text[:300]}")
        return self.response(parsed, request)

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        if not self._config.api_key:
            raise self._error("API key is not configured")
        body = self.body(request)
        client = await self._get_client()
        attempts = self._config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(client, body, request)
            except ProviderError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                delay = (
                    self._BACKOFF_BASE_SECONDS
                    * attempt
                    * (1 + random.uniform(-0.2, 0.2))
                )
                logger.warning(
                    f"OpenRouter attempt {attempt}/{attempts} failed ({exc}), "
                    f"retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
