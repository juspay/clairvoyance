"""Typed HTTP adapter for Supermemory's extracted-memory APIs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.dynamic import SUPERMEMORY_API_KEY
from app.core.config.static import SUPERMEMORY_BASE_URL
from app.core.transport.http_client import create_aiohttp_session

_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class SupermemoryError(RuntimeError):
    pass


class SupermemoryRetryableError(SupermemoryError):
    pass


class SupermemoryPermanentError(SupermemoryError):
    pass


class SupermemoryClient:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or SUPERMEMORY_BASE_URL).rstrip("/")
        self._session = session
        self._api_key_override = api_key

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = create_aiohttp_session(timeout=_TIMEOUT)
        return self._session

    async def _request(
        self, method: str, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        api_key = self._api_key_override or await SUPERMEMORY_API_KEY()
        if not api_key:
            raise SupermemoryPermanentError("Supermemory API key is not configured")
        try:
            async with self._get_session().request(
                method,
                f"{self._base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                body = await response.text()
                if 200 <= response.status < 300:
                    if not body:
                        return {}
                    try:
                        data = await response.json()
                    except (ValueError, aiohttp.ContentTypeError) as error:
                        raise SupermemoryRetryableError(
                            "Supermemory returned invalid JSON"
                        ) from error
                    return data if isinstance(data, dict) else {}
                message = f"Supermemory request failed with status {response.status}"
                if response.status == 429 or response.status >= 500:
                    raise SupermemoryRetryableError(message)
                raise SupermemoryPermanentError(message)
        except SupermemoryError:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise SupermemoryRetryableError(
                f"Supermemory transport failed: {type(error).__name__}"
            ) from error

    async def create_memories(
        self,
        memories: List[Dict[str, Any]],
        container_tag: str,
    ) -> List[Dict[str, Any]]:
        response = await self._request(
            "POST",
            "/v4/memories",
            {"memories": memories, "containerTag": container_tag},
        )
        return [item for item in response.get("memories", []) if isinstance(item, dict)]

    async def update_memory(
        self,
        *,
        memory_id: str,
        container_tag: str,
        new_content: str,
        metadata: Dict[str, Any],
        forget_after: str,
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            "/v4/memories",
            {
                "id": memory_id,
                "containerTag": container_tag,
                "newContent": new_content,
                "metadata": metadata,
                "forgetAfter": forget_after,
                "forgetReason": "configured memory retention",
            },
        )

    async def forget_memory(
        self, *, memory_id: str, container_tag: str, reason: str
    ) -> None:
        await self._request(
            "DELETE",
            "/v4/memories",
            {
                "id": memory_id,
                "containerTag": container_tag,
                "reason": reason,
            },
        )

    async def search_memories(
        self, query: str, container_tag: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        response = await self._request(
            "POST",
            "/v4/search",
            {
                "q": query,
                "containerTag": container_tag,
                "searchMode": "memories",
                "limit": min(100, max(2, limit)),
            },
        )
        return [item for item in response.get("results", []) if isinstance(item, dict)]

    async def merge_container_tags(
        self, source_tag: str, target_tag: str
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/v3/container-tags/merge",
            {
                "containerTags": [source_tag, target_tag],
                "targetContainerTag": target_tag,
            },
        )
