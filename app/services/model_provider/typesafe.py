"""TypeSafe (System One) — a judge model behind a task-specific API.

System One is not a chat API: it takes a structured payload (``state`` and
``questions``) and answers in its own fixed shape (``answers``, ``usage``,
``model``). So the request's ``input`` must be a mapping — a conversation
raises — and it is posted as the body with the ``model`` added. The reply
is always structured: ``structured`` is the vendor's JSON object.

``system_prompt``, ``schema`` and ``settings`` mean nothing to this API and
are ignored; ``extra`` is passed through like any provider's.

The endpoint resolves to US-West; unpooled calls pay ~850 ms of TLS on top
of ~380 ms round trip, so the pooled client is what keeps the warm path
affordable.
"""

import asyncio
import random
from typing import Any, ClassVar, Dict, Mapping, Optional

import httpx

from app.core.logger import logger
from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    ProviderConfig,
    ProviderError,
    Usage,
)


class TypeSafeProvider:
    name = "typesafe"

    _BACKOFF_BASE_SECONDS: ClassVar[float] = 2.0

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
                            max_connections=10, max_keepalive_connections=5
                        ),
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # --- request / response: PURE -----------------------------------------------

    def body(self, request: GenerateRequest) -> Dict[str, object]:
        """The payload plus the model. A conversation ``input`` raises."""
        if not isinstance(request.input, Mapping):
            raise self._error("input must be a mapping (state, questions)")
        return {
            **(request.extra or {}),
            **request.input,
            "model": request.model,
        }

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

    def response(
        self, text: str, parsed: Mapping[str, Any], request: GenerateRequest
    ) -> GenerateResponse:
        usage = parsed.get("usage")
        return GenerateResponse(
            content=text,
            model=str(parsed.get("model") or request.model),
            structured=dict(parsed),
            usage=(
                Usage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
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
        return self.response(reply.text, parsed, request)

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
                    f"TypeSafe attempt {attempt}/{attempts} failed ({exc}), "
                    f"retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
