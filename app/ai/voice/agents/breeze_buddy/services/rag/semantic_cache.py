"""
In-process FAISS semantic cache for the Breeze Buddy RAG service.

The cache is the bridge between the SlowThinker (writer) and the FastTalker
(reader).  Entries are indexed by the embedding of the retrieval query used
when they were cached, and the cached value is the pre-fetched chunk/context
associated with that query.  Semantically similar future queries reuse the
same prefetched result.

Design decisions
----------------
* **Inner-product index over normalised vectors** – equivalent to cosine sim,
  faster than L2.
* **Async lock on both reads and writes** – SlowThinker writes and FastTalker
  reads run concurrently in separate asyncio tasks.  ``_rebuild_index()``
  replaces ``self._index`` and ``self._entries`` in place, so an unguarded
  ``get()`` can race with a concurrent ``put()``/eviction and crash or return
  stale indices.  Both paths acquire ``self._lock``.
* **LRU eviction** – when at capacity the entry with the oldest
  ``last_accessed`` timestamp is removed.
* **TTL eviction** – expired entries are purged lazily on each ``put``.
* **Rebuild-on-evict** – FAISS flat indexes do not support deletion, so the
  index is rebuilt when entries are removed.  Because the cache is small
  (default 1000 entries) this is fast (< 0.1 ms for 1000 × 1536-dim).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import faiss
import numpy as np

from app.core.logger import logger


@dataclass
class CachedContext:
    """One entry in the semantic cache."""

    text: str
    metadata: Dict
    embedding: np.ndarray
    relevance_score: float
    ttl: float
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl


class SemanticCache:
    """FAISS-backed in-memory semantic cache.

    Args:
        dimension: Embedding dimensionality (must match the vector store).
        max_size: Maximum number of cached entries.
        default_ttl: Default TTL in seconds for new entries.
        similarity_threshold: Minimum cosine similarity to count as a cache hit.
    """

    def __init__(
        self,
        dimension: int,
        max_size: int = 1000,
        default_ttl: float = 300.0,
        similarity_threshold: float = 0.40,
    ) -> None:
        self._dimension = dimension
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._similarity_threshold = similarity_threshold
        self._lock = asyncio.Lock()

        self._entries: List[CachedContext] = []
        self._index = faiss.IndexFlatIP(dimension)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def put(
        self,
        query_embedding: np.ndarray,
        text: str,
        metadata: Optional[Dict] = None,
        relevance_score: float = 1.0,
        ttl: Optional[float] = None,
    ) -> None:
        """Insert or refresh a single cache entry.

        If a near-identical vector (cosine ≥ 0.95) is already cached, we
        refresh it in place instead of adding a duplicate.

        Args:
            query_embedding: Embedding used as the cache key.
            text: Chunk text.
            metadata: Source metadata.
            relevance_score: Retrieval score from the vector store.
            ttl: Override the default TTL.
        """
        async with self._lock:
            self._put_locked(query_embedding, text, metadata, relevance_score, ttl)

    async def put_batch(
        self,
        entries: List[Dict[str, Any]],
    ) -> None:
        """Insert multiple cache entries under a single lock acquisition.

        Each element of *entries* is a dict with keys:
          ``query_embedding``, ``text``, ``metadata`` (optional),
          ``relevance_score`` (optional), ``ttl`` (optional).

        This is more efficient than calling ``put()`` N times because the lock
        is acquired only once.
        """
        async with self._lock:
            for e in entries:
                self._put_locked(
                    e["query_embedding"],
                    e["text"],
                    e.get("metadata"),
                    e.get("relevance_score", 1.0),
                    e.get("ttl"),
                )

    # ------------------------------------------------------------------
    # Read path (lock-protected — SlowThinker writes and FastTalker reads
    # run as concurrent asyncio tasks; _rebuild_index() replaces _index
    # and _entries in place, so reads must hold the same lock as writes)
    # ------------------------------------------------------------------

    async def get(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        similarity_threshold: Optional[float] = None,
    ) -> List[CachedContext]:
        """Look up cached context for a query embedding.

        Args:
            query_embedding: The embedding of the user's current utterance.
            top_k: Maximum results to return.
            similarity_threshold: Override the instance-level threshold.

        Returns:
            List of non-expired ``CachedContext`` entries sorted by relevance
            descending, or an empty list on a cache miss.
        """
        threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else self._similarity_threshold
        )

        q = query_embedding.reshape(1, -1).astype(np.float32).copy()
        faiss.normalize_L2(q)

        async with self._lock:
            if self._index.ntotal == 0:
                return []

            k = min(top_k, self._index.ntotal)
            scores, idxs = self._index.search(q, k)  # type: ignore[call-arg]

            now = time.time()
            hits: List[CachedContext] = []
            for score, idx in zip(scores[0], idxs[0]):
                if idx == -1 or idx >= len(self._entries):
                    continue
                entry = self._entries[idx]
                if entry.is_expired:
                    continue
                if score >= threshold:
                    entry.last_accessed = now
                    hits.append(entry)

        if hits:
            logger.debug(
                "SemanticCache HIT: %d chunks (best=%.3f)", len(hits), scores[0][0]
            )
        else:
            logger.debug(
                "SemanticCache MISS (best=%.3f < threshold=%.3f)",
                scores[0][0] if scores[0][0] > -1 else 0.0,
                threshold,
            )

        return sorted(hits, key=lambda e: e.relevance_score, reverse=True)

    # ------------------------------------------------------------------
    # Internal (must be called with lock held)
    # ------------------------------------------------------------------

    def _put_locked(
        self,
        query_embedding: np.ndarray,
        text: str,
        metadata: Optional[Dict],
        relevance_score: float,
        ttl: Optional[float],
    ) -> None:
        # Check for near-duplicate
        if self._index.ntotal > 0:
            q = query_embedding.reshape(1, -1).astype(np.float32).copy()
            faiss.normalize_L2(q)
            scores, idxs = self._index.search(q, 1)  # type: ignore[call-arg]
            if scores[0][0] > 0.95 and idxs[0][0] != -1:
                idx = int(idxs[0][0])
                if idx < len(self._entries):
                    self._entries[idx].text = text
                    self._entries[idx].relevance_score = relevance_score
                    self._entries[idx].created_at = time.time()
                    self._entries[idx].last_accessed = time.time()
                    return  # refreshed — done

        # Evict expired entries before adding
        self._evict_expired()

        # Evict LRU if still at capacity
        if len(self._entries) >= self._max_size:
            self._evict_lru()

        # Add entry
        emb = query_embedding.reshape(1, -1).astype(np.float32).copy()
        faiss.normalize_L2(emb)
        self._index.add(emb)  # type: ignore[call-arg]
        self._entries.append(
            CachedContext(
                text=text,
                metadata=metadata or {},
                embedding=query_embedding.copy(),
                relevance_score=relevance_score,
                ttl=ttl if ttl is not None else self._default_ttl,
            )
        )

    def _evict_expired(self, max_age: Optional[float] = None) -> int:
        now = time.time()
        to_keep: List[int] = []
        removed = 0
        for i, entry in enumerate(self._entries):
            expired = entry.is_expired
            if max_age is not None:
                expired = expired or (now - entry.created_at) > max_age
            if not expired:
                to_keep.append(i)
            else:
                removed += 1
        if removed:
            self._rebuild_index(to_keep)
        return removed

    def _evict_lru(self) -> None:
        if not self._entries:
            return
        lru_idx = min(
            range(len(self._entries)), key=lambda i: self._entries[i].last_accessed
        )
        self._rebuild_index([i for i in range(len(self._entries)) if i != lru_idx])

    def _rebuild_index(self, keep_indices: List[int]) -> None:
        self._entries = [self._entries[i] for i in keep_indices]
        self._index = faiss.IndexFlatIP(self._dimension)
        if self._entries:
            vectors = np.stack([e.embedding for e in self._entries]).astype(np.float32)
            faiss.normalize_L2(vectors)
            self._index.add(vectors)  # type: ignore[call-arg]
