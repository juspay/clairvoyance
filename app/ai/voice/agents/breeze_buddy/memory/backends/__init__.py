"""Pluggable memory backends + selection factory.

The active backend is chosen by name: an explicit per-template override falls
back to the global ``BUDDY_MEMORY_BACKEND`` env, which defaults to ``pgvector``.
Unknown names fall back to ``pgvector`` so a misconfiguration never disables
memory silently in a surprising way.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Optional, Type

from app.ai.voice.agents.breeze_buddy.memory.backends.base import (
    MemoryBackend,
    MemoryIdentity,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.pgvector import (
    PgVectorMemoryBackend,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.supermemory import (
    SupermemoryMemoryBackend,
)
from app.core.config.static import BUDDY_MEMORY_BACKEND

_REGISTRY: Dict[str, Type[MemoryBackend]] = {
    PgVectorMemoryBackend.name: PgVectorMemoryBackend,
    SupermemoryMemoryBackend.name: SupermemoryMemoryBackend,
}

_DEFAULT = PgVectorMemoryBackend.name


@lru_cache(maxsize=None)
def get_memory_backend(name: Optional[str] = None) -> MemoryBackend:
    """Return a (cached) backend instance for ``name`` or the configured default."""
    key = (name or BUDDY_MEMORY_BACKEND or _DEFAULT).lower()
    cls = _REGISTRY.get(key) or _REGISTRY[_DEFAULT]
    return cls()


__all__ = [
    "MemoryBackend",
    "MemoryIdentity",
    "PgVectorMemoryBackend",
    "SupermemoryMemoryBackend",
    "get_memory_backend",
]
