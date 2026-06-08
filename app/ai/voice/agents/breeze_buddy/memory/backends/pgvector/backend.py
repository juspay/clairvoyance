"""Postgres/pgvector storage adapter for extracted memory facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, List

from app.ai.voice.agents.breeze_buddy.memory.backends.base import MemoryBackend
from app.database.accessor.breeze_buddy.user_memory import (
    PreparedMemoryOperation,
    apply_memory_operations,
    list_user_memories,
    merge_identity_records,
    search_user_memories,
)
from app.schemas.breeze_buddy.memory import (
    MemoryFact,
    MemoryIdentity,
    MemoryOperation,
)
from app.schemas.embeddings import EmbeddingConfig
from app.services.embeddings import get_embedding_provider


class PgVectorMemoryBackend(MemoryBackend):
    name: ClassVar[str] = "pgvector"

    async def list_facts(
        self, identity: MemoryIdentity, limit: int = 20
    ) -> List[MemoryFact]:
        memories = await list_user_memories(
            identity.reseller_id,
            identity.merchant_id,
            identity.customer_key,
            limit,
        )
        return [memory.as_fact() for memory in memories]

    async def apply_operations(
        self,
        identity: MemoryIdentity,
        operations: List[MemoryOperation],
        *,
        source_channel: str,
        operation_key: str,
        retention_days: int,
        max_facts: int,
        embedding_config: EmbeddingConfig,
    ) -> None:
        if not operations:
            return

        embedded_indexes = [
            index
            for index, operation in enumerate(operations)
            if operation.op != "DELETE"
        ]
        texts = [operations[index].fact for index in embedded_indexes]
        embeddings = (
            await get_embedding_provider(embedding_config).embed(
                texts, input_type="document"
            )
            if texts
            else []
        )
        vectors = dict(zip(embedded_indexes, embeddings))
        prepared = [
            PreparedMemoryOperation(
                operation=operation,
                embedding=vectors.get(index),
                operation_key=f"{operation_key}:{index}",
            )
            for index, operation in enumerate(operations)
        ]
        await apply_memory_operations(
            identity,
            prepared,
            source_channel=source_channel,
            expires_at=datetime.now(timezone.utc) + timedelta(days=retention_days),
            max_facts=max_facts,
        )

    async def search(
        self,
        identity: MemoryIdentity,
        query: str,
        *,
        embedding_config: EmbeddingConfig,
        k: int = 5,
    ) -> List[MemoryFact]:
        vectors = await get_embedding_provider(embedding_config).embed(
            [query], input_type="query"
        )
        if not vectors:
            return await self.list_facts(identity, k)
        memories = await search_user_memories(
            identity.reseller_id,
            identity.merchant_id,
            identity.customer_key,
            vectors[0],
            k,
        )
        return [memory.as_fact() for memory in memories]

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        if not identity.phone or not identity.explicit_customer_id:
            return identity
        canonical, _ = await merge_identity_records(identity)
        return canonical
