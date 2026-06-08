"""Supermemory adapter that stores extracted facts, never raw transcripts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends.base import MemoryBackend
from app.ai.voice.agents.breeze_buddy.memory.backends.supermemory.client import (
    SupermemoryClient,
)
from app.database.accessor.breeze_buddy.user_memory import merge_identity_records
from app.schemas.breeze_buddy.memory import (
    MemoryFact,
    MemoryIdentity,
    MemoryOperation,
    MemoryUpdateOperation,
)
from app.schemas.embeddings import EmbeddingConfig

_PROFILE_QUERY = "durable user preferences, attributes, outcomes, and ongoing context"


def _fact_from_result(result: Dict[str, Any]) -> Optional[MemoryFact]:
    text = result.get("memory") or result.get("chunk")
    if not isinstance(text, str) or not text.strip():
        return None
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    category = metadata.get("category")
    if category not in {"preference", "attribute", "outcome", "context"}:
        category = None
    confidence = metadata.get("confidence", 1.0)
    return MemoryFact(
        id=str(result["id"]) if result.get("id") else None,
        fact=text.strip(),
        category=category,
        structured={},
        confidence=confidence if isinstance(confidence, (int, float)) else 1.0,
        source_channel=(
            metadata.get("source_channel")
            if metadata.get("source_channel") in {"voice", "chat"}
            else None
        ),
    )


class SupermemoryMemoryBackend(MemoryBackend):
    name: ClassVar[str] = "supermemory"

    def __init__(self, client: Optional[SupermemoryClient] = None) -> None:
        self._client = client or SupermemoryClient()

    async def list_facts(
        self, identity: MemoryIdentity, limit: int = 20
    ) -> List[MemoryFact]:
        results = await self._client.search_memories(
            _PROFILE_QUERY, identity.scope_tag, limit
        )
        facts = [_fact_from_result(result) for result in results]
        return [fact for fact in facts if fact is not None][:limit]

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
        del max_facts, embedding_config
        forget_after = (
            datetime.now(timezone.utc) + timedelta(days=retention_days)
        ).isoformat()

        for index, operation in enumerate(operations):
            op_key = f"{operation_key}:{index}"
            if operation.op == "DELETE":
                existing = await self._find_exact(identity, operation.fact)
                if existing and existing.id:
                    await self._client.forget_memory(
                        memory_id=existing.id,
                        container_tag=identity.scope_tag,
                        reason="memory curator delete",
                    )
                continue

            target = (
                operation.supersedes_fact
                if isinstance(operation, MemoryUpdateOperation)
                else operation.fact
            )
            existing_result = await self._find_exact_result(identity, target, op_key)
            metadata = {
                "operation_key": op_key,
                "source_channel": source_channel,
                "category": operation.category or "",
                "confidence": operation.confidence,
                "expiry_applied": True,
                "expires_at": forget_after,
            }

            if existing_result and existing_result.get("id"):
                current_metadata = existing_result.get("metadata")
                if (
                    isinstance(current_metadata, dict)
                    and current_metadata.get("operation_key") == op_key
                    and current_metadata.get("expiry_applied") is True
                ):
                    continue
                await self._client.update_memory(
                    memory_id=str(existing_result["id"]),
                    container_tag=identity.scope_tag,
                    new_content=operation.fact,
                    metadata=metadata,
                    forget_after=forget_after,
                )
                continue

            created = await self._client.create_memories(
                [
                    {
                        "content": operation.fact,
                        "isStatic": False,
                        "metadata": {**metadata, "expiry_applied": False},
                    }
                ],
                identity.scope_tag,
            )
            if not created or not created[0].get("id"):
                raise RuntimeError("Supermemory create returned no memory ID")
            await self._client.update_memory(
                memory_id=str(created[0]["id"]),
                container_tag=identity.scope_tag,
                new_content=operation.fact,
                metadata=metadata,
                forget_after=forget_after,
            )

    async def search(
        self,
        identity: MemoryIdentity,
        query: str,
        *,
        embedding_config: EmbeddingConfig,
        k: int = 5,
    ) -> List[MemoryFact]:
        del embedding_config
        results = await self._client.search_memories(query, identity.scope_tag, k)
        facts = [_fact_from_result(result) for result in results]
        return [fact for fact in facts if fact is not None][:k]

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        if not identity.phone or not identity.explicit_customer_id:
            return identity
        canonical, _ = await merge_identity_records(identity)
        provisional = identity.model_copy(
            update={
                "customer_key": f"phone:{identity.phone}",
                "key_type": "phone",
            }
        )
        if provisional.scope_tag != canonical.scope_tag:
            await self._client.merge_container_tags(
                provisional.scope_tag, canonical.scope_tag
            )
        return canonical

    async def _find_exact(
        self, identity: MemoryIdentity, fact: str
    ) -> Optional[MemoryFact]:
        result = await self._find_exact_result(identity, fact, None)
        return _fact_from_result(result) if result else None

    async def _find_exact_result(
        self,
        identity: MemoryIdentity,
        fact: str,
        operation_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        results = await self._client.search_memories(fact, identity.scope_tag, 10)
        normalized = fact.strip().casefold()
        for result in results:
            metadata = result.get("metadata")
            if (
                operation_key
                and isinstance(metadata, dict)
                and metadata.get("operation_key") == operation_key
            ):
                return result
            text = result.get("memory") or result.get("chunk")
            if isinstance(text, str) and text.strip().casefold() == normalized:
                return result
        return None
