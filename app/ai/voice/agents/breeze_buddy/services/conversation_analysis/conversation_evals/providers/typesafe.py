"""TypeSafe (System One) as a CONVERSATION_EVALS provider — the only door to
the vendor endpoint.

Everything TypeSafe lives in this class: the pooled HTTP client, the retry
policy, the error type. ONE pool per process, shared by every instance, is
what makes the warm path affordable (the endpoint resolves to US-West;
unpooled calls pay ~850 ms of TLS on top of ~380 ms round trip). Retries
cover only what a retry can fix (429/529 and 5xx, with jittered backoff);
everything else surfaces as ``TypeSafeError`` and the calling site applies
its own fail posture — for the conversation evaluation that is
log-and-skip, never blocking the call record.
"""

import asyncio
import random
from typing import Any, ClassVar, Dict, FrozenSet, Optional

import httpx

from app.core.config.static import (
    TYPESAFE_API_KEY,
    TYPESAFE_API_URL,
    TYPESAFE_TIMEOUT_SECONDS,
)
from app.core.logger import logger


class TypeSafeError(Exception):
    """The judgment call failed after retries; the site decides what that means."""


class TypeSafeProvider:
    name = "typesafe"

    _RETRYABLE_STATUS: ClassVar[FrozenSet[int]] = frozenset({429, 500, 502, 503, 529})
    _MAX_ATTEMPTS: ClassVar[int] = 3
    _BACKOFF_BASE_SECONDS: ClassVar[float] = 2.0

    # the one pooled client per process, shared by every instance
    _client: ClassVar[Optional[httpx.AsyncClient]] = None
    _client_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    async def _get_client(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            async with cls._client_lock:
                if cls._client is None or cls._client.is_closed:
                    cls._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(TYPESAFE_TIMEOUT_SECONDS),
                        limits=httpx.Limits(
                            max_connections=10, max_keepalive_connections=5
                        ),
                    )
        return cls._client

    async def close(self) -> None:
        """Drain the shared pool (lifespan shutdown); safe when never opened."""
        cls = type(self)
        if cls._client is not None and not cls._client.is_closed:
            await cls._client.aclose()
        cls._client = None

    async def judge(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
        model: str,
    ) -> Dict[str, Any]:
        """POST one state + typed questions, return the parsed response body
        (``answers``, ``usage``, ``model``). Raises ``TypeSafeError`` on a
        missing key, a non-retryable status, or exhausted retries."""
        if not TYPESAFE_API_KEY:
            raise TypeSafeError("TYPESAFE_API_KEY is not configured")

        client = await self._get_client()
        last_error = ""
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    TYPESAFE_API_URL,
                    headers={"Authorization": f"Bearer {TYPESAFE_API_KEY}"},
                    json={"state": state, "model": model, "questions": questions},
                )
            except httpx.HTTPError as exc:
                last_error = f"transport error: {type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in self._RETRYABLE_STATUS:
                    raise TypeSafeError(last_error)

            if attempt < self._MAX_ATTEMPTS:
                delay = (
                    self._BACKOFF_BASE_SECONDS
                    * attempt
                    * (1 + random.uniform(-0.2, 0.2))
                )
                logger.warning(
                    f"TypeSafe request attempt {attempt}/{self._MAX_ATTEMPTS} "
                    f"failed ({last_error}), retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        raise TypeSafeError(f"failed after {self._MAX_ATTEMPTS} attempts: {last_error}")
