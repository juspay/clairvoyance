"""Backend contract for already-extracted durable memory facts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, List

from app.schemas.breeze_buddy.memory import (
    MemoryFact,
    MemoryIdentity,
    MemoryOperation,
)
from app.schemas.embeddings import EmbeddingConfig


class MemoryBackend(ABC):
    """Storage adapter; extraction is intentionally backend-neutral."""

    name: ClassVar[str]

    @abstractmethod
    async def list_facts(
        self, identity: MemoryIdentity, limit: int = 20
    ) -> List[MemoryFact]:
        """Return current, non-expired facts in profile order."""

    @abstractmethod
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
        """Apply one idempotent curator result or raise for queue retry."""

    @abstractmethod
    async def search(
        self,
        identity: MemoryIdentity,
        query: str,
        *,
        embedding_config: EmbeddingConfig,
        k: int = 5,
    ) -> List[MemoryFact]:
        """Semantic recall over this customer's current facts."""

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        return identity
