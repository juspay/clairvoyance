"""Fail-open facade for resolved persistent-memory runtime state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends import (
    MemoryBackend,
    get_memory_backend,
)
from app.ai.voice.agents.breeze_buddy.memory.queue import MemoryQueue
from app.ai.voice.agents.breeze_buddy.memory.render import render_memory_user_tail
from app.ai.voice.agents.breeze_buddy.memory.runtime import ResolvedMemoryRuntime
from app.core.logger import logger
from app.schemas.breeze_buddy.memory import MemoryExtractionJob, MemoryFact


class MemoryService:
    def __init__(
        self,
        runtime: Optional[ResolvedMemoryRuntime],
        backend: Optional[MemoryBackend] = None,
        queue: Optional[MemoryQueue] = None,
    ) -> None:
        self._runtime = runtime
        self._backend = (
            (backend or get_memory_backend(runtime.backend))
            if runtime is not None
            else None
        )
        self._queue = queue

    @property
    def enabled(self) -> bool:
        return self._runtime is not None and self._backend is not None

    async def list_facts(self, max_facts: Optional[int] = None) -> List[MemoryFact]:
        if not self._runtime or not self._backend:
            return []
        try:
            return await self._backend.list_facts(
                self._runtime.identity,
                max_facts or self._runtime.max_facts,
            )
        except Exception as error:
            logger.warning(
                "[memory.service] fact read failed open "
                f"(scope={self._runtime.identity.scope_digest[:12]}, "
                f"error={type(error).__name__})"
            )
            return []

    async def get_user_tail_block(
        self, max_facts: Optional[int] = None
    ) -> Optional[str]:
        return render_memory_user_tail(await self.list_facts(max_facts))

    async def get_profile_block(self, max_facts: Optional[int] = None) -> Optional[str]:
        """Compatibility alias; returned data is safe only in a user-role tail."""
        return await self.get_user_tail_block(max_facts)

    async def search(self, query: str, k: int = 5) -> List[MemoryFact]:
        if not self._runtime or not self._backend or not query.strip():
            return []
        try:
            return await self._backend.search(
                self._runtime.identity,
                query,
                embedding_config=self._runtime.engine.embedding,
                k=k,
            )
        except Exception as error:
            logger.warning(
                "[memory.service] search failed open "
                f"(scope={self._runtime.identity.scope_digest[:12]}, "
                f"error={type(error).__name__})"
            )
            return []

    async def enqueue_extraction(
        self,
        *,
        kind: Literal["voice_lead", "chat_session"],
        record_id: str,
        source_channel: Literal["voice", "chat"],
        idempotency_key: Optional[str] = None,
    ) -> bool:
        """Atomically enqueue a source-record reference; never stores transcripts."""
        if not self._runtime or not self._backend:
            return False
        try:
            queue = self._queue or await MemoryQueue.create()
            stable_key = idempotency_key or f"{kind}:{record_id}"
            job = MemoryExtractionJob(
                kind=kind,
                record_id=record_id,
                identity=self._runtime.identity,
                source_channel=source_channel,
                backend=self._runtime.backend,
                retention_days=self._runtime.engine.retention_days,
                max_facts=self._runtime.max_facts,
                embedding=self._runtime.engine.embedding,
                idempotency_key=stable_key,
                attempt=0,
                enqueued_at=datetime.now(timezone.utc),
            )
            enqueued = await queue.enqueue(job)
            logger.debug(
                "[memory.service] extraction enqueue result "
                f"(job={job.identity.scope_digest[:12]}, enqueued={enqueued})"
            )
            return enqueued
        except Exception as error:
            logger.warning(
                "[memory.service] enqueue failed open "
                f"(error={type(error).__name__})"
            )
            return False
