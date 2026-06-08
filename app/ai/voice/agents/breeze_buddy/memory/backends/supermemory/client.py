"""Thin async wrapper around the official supermemory.ai Python SDK.

Uses `supermemory.AsyncSupermemory` (httpx-based, typed Pydantic responses)
but routes through our shared proxy-aware httpx client so calls honour the AWS
proxy config. Best-effort: every call logs and swallows its own errors,
returning an empty/falsey result so a memory failure never breaks a call.

SDK method map (confirmed against supermemory==3.x / api.supermemory.ai):
  client.add(content, container_tags=[tag], metadata)   -> POST /v3/documents
  client.search.memories(q, container_tag=tag, limit)   -> POST /v4/search  (extracted facts)
  client.documents.list(container_tags=[tag], limit)    -> POST /v3/documents/list
  client.documents.update(id, container_tags=[tag])     -> PATCH /v3/documents/{id}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.config.static import SUPERMEMORY_API_KEY, SUPERMEMORY_BASE_URL
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

try:
    from supermemory import AsyncSupermemory

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    AsyncSupermemory = None  # type: ignore[assignment,misc]

_TIMEOUT_SECONDS = 15.0
_MAX_RETRIES = 2


class SupermemoryClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or SUPERMEMORY_API_KEY
        self._base_url = (base_url or SUPERMEMORY_BASE_URL).rstrip("/")
        self._sdk: Optional[Any] = None

    @property
    def configured(self) -> bool:
        return bool(_SDK_AVAILABLE and self._api_key and self._base_url)

    def _client(self) -> Any:
        """Lazily build a reusable AsyncSupermemory bound to our proxy-aware httpx client."""
        if self._sdk is None:
            if AsyncSupermemory is None:
                raise RuntimeError("supermemory SDK is not installed")
            self._sdk = AsyncSupermemory(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=_MAX_RETRIES,
                http_client=create_http_client(timeout=_TIMEOUT_SECONDS),
            )
        return self._sdk

    def _ready(self, op: str) -> bool:
        if not self.configured:
            logger.warning(
                f"[memory.supermemory] not configured "
                f"(sdk={_SDK_AVAILABLE}, key={'set' if self._api_key else 'unset'}); "
                f"skipping {op}"
            )
            return False
        return True

    async def add(
        self,
        content: str,
        container_tags: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        if not self._ready("add"):
            return None
        try:
            return await self._client().add(
                content=content,
                container_tags=container_tags,
                metadata=metadata or {},
            )
        except Exception as e:
            logger.warning(f"[memory.supermemory] add failed: {e}")
            return None

    async def search_memories(
        self, q: str, container_tag: str, limit: int = 5
    ) -> List[Any]:
        """Search extracted memory facts (v4) scoped to one container tag."""
        if not self._ready("search_memories"):
            return []
        try:
            resp = await self._client().search.memories(
                q=q, container_tag=container_tag, limit=limit
            )
            return list(getattr(resp, "results", None) or [])
        except Exception as e:
            logger.warning(f"[memory.supermemory] search_memories failed: {e}")
            return []

    async def list_documents(
        self, container_tags: List[str], limit: int = 100
    ) -> List[Any]:
        if not self._ready("list_documents"):
            return []
        try:
            resp = await self._client().documents.list(
                container_tags=container_tags, limit=limit
            )
            return list(getattr(resp, "memories", None) or [])
        except Exception as e:
            logger.warning(f"[memory.supermemory] list_documents failed: {e}")
            return []

    async def update_document(
        self, document_id: str, container_tags: List[str]
    ) -> Optional[Any]:
        if not self._ready("update_document"):
            return None
        try:
            return await self._client().documents.update(
                document_id, container_tags=container_tags
            )
        except Exception as e:
            logger.warning(f"[memory.supermemory] update_document failed: {e}")
            return None
