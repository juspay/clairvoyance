"""MemoryService: thin facade over the selected memory backend.

Read (`get_profile_block`, `search`) and merge delegate straight to a
`MemoryBackend`. Write is decoupled via a Redis queue drained by the worker —
`enqueue_extraction` is backend-agnostic infra that just stamps which backend
the caller chose so the worker can reconstruct it.
"""

from __future__ import annotations

import json
from typing import List, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends import (
    MemoryBackend,
    MemoryIdentity,
    get_memory_backend,
)
from app.core.logger import logger
from app.services.redis.client import get_redis_service

_QUEUE_KEY = "memory:extract:queue"
# Whichever end-path enqueues first wins this flag; the other is a no-op.
# 2h TTL comfortably covers idle-cleanup lag without being permanent.
_DEDUP_TTL_SECONDS = 7200


class MemoryService:
    def __init__(self, backend: Optional[MemoryBackend] = None) -> None:
        self._backend = backend or get_memory_backend()

    async def get_profile_block(
        self,
        reseller_id: str,
        merchant_id: str,
        customer_key: str,
        key_type: str = "customer_id",
        max_facts: int = 20,
    ) -> Optional[str]:
        """Return a <user_memory> block for injection into role_messages, or None."""
        identity = MemoryIdentity(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            customer_key=customer_key,
            key_type=key_type,
        )
        return await self._backend.get_profile_block(identity, max_facts)

    async def search(
        self,
        reseller_id: str,
        merchant_id: str,
        customer_key: str,
        query: str,
        key_type: str = "customer_id",
        k: int = 5,
    ) -> List[str]:
        """Phase 2: semantic recall over the user's own facts."""
        identity = MemoryIdentity(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            customer_key=customer_key,
            key_type=key_type,
        )
        return await self._backend.search(identity, query, k)

    async def enqueue_extraction(
        self,
        kind: str,
        record_id: str,
        customer_key: str,
        key_type: str,
        reseller_id: str,
        merchant_id: str,
        source_channel: str,
        phone: Optional[str] = None,
        explicit_customer_id: Optional[str] = None,
        backend: Optional[str] = None,
        extraction_prompt: Optional[str] = None,
    ) -> None:
        """Push an extraction job onto the Redis queue.

        Idempotent per (kind, record_id) via a Redis SET NX guard — both the
        user-triggered end and the idle sweep can reach this for the same
        record. Best-effort: any failure is logged and swallowed so the caller
        (end_conversation / end_chat_session) is never blocked.
        """
        try:
            redis = await get_redis_service()
            client = await redis.get_client()

            dedup_key = f"memory:extract:dedup:{kind}:{record_id}"
            won = await client.set(  # type: ignore[union-attr]
                dedup_key, "1", nx=True, ex=_DEDUP_TTL_SECONDS
            )
            if not won:
                logger.debug(
                    f"[memory.service] extraction already enqueued, skipping: "
                    f"kind={kind} id={record_id}"
                )
                return

            payload = json.dumps(
                {
                    "kind": kind,
                    "record_id": record_id,
                    "customer_key": customer_key,
                    "key_type": key_type,
                    "reseller_id": reseller_id,
                    "merchant_id": merchant_id,
                    "source_channel": source_channel,
                    "phone": phone,
                    "explicit_customer_id": explicit_customer_id,
                    "backend": backend,
                    "extraction_prompt": extraction_prompt,
                }
            )
            await client.rpush(_QUEUE_KEY, payload)  # type: ignore[union-attr]
            logger.debug(
                f"[memory.service] enqueued extraction: "
                f"kind={kind} id={record_id} key={customer_key!r} backend={backend}"
            )
        except Exception as e:
            logger.warning(
                f"[memory.service] enqueue_extraction failed "
                f"(kind={kind} id={record_id}): {e}"
            )
