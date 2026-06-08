"""Strict pluggable memory-backend registry."""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Type

from app.ai.voice.agents.breeze_buddy.memory.backends.base import MemoryBackend
from app.ai.voice.agents.breeze_buddy.memory.backends.pgvector import (
    PgVectorMemoryBackend,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.supermemory import (
    SupermemoryMemoryBackend,
)
from app.schemas.breeze_buddy.memory import MemoryBackendName, MemoryIdentity

_REGISTRY: Dict[str, Type[MemoryBackend]] = {
    PgVectorMemoryBackend.name: PgVectorMemoryBackend,
    SupermemoryMemoryBackend.name: SupermemoryMemoryBackend,
}


@lru_cache(maxsize=None)
def get_memory_backend(name: MemoryBackendName) -> MemoryBackend:
    """Return a cached backend instance; unknown names are configuration errors."""
    key = name.lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise ValueError(
            f"Unknown memory backend {name!r}; available={sorted(_REGISTRY)}"
        )
    return cls()


__all__ = [
    "MemoryBackend",
    "MemoryIdentity",
    "PgVectorMemoryBackend",
    "SupermemoryMemoryBackend",
    "get_memory_backend",
]
