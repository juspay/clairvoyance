"""
FastTalker – foreground retrieval agent for Breeze Buddy RAG.

Reads context from the semantic cache (sub-ms) and falls back to a direct
vector-store search only on a cache miss.  The retrieved context is formatted
as a structured string that can be injected directly into the system / task
prompt.
"""

from __future__ import annotations

from typing import List

from app.ai.voice.agents.breeze_buddy.services.rag.embeddings import EmbeddingProvider
from app.ai.voice.agents.breeze_buddy.services.rag.semantic_cache import SemanticCache
from app.ai.voice.agents.breeze_buddy.services.rag.types import RagMetrics
from app.ai.voice.agents.breeze_buddy.services.rag.vector_store import PgVectorStore
from app.core.logger import logger


class FastTalker:
    """Foreground RAG retrieval agent optimised for minimum latency.

    Cache-hit path: ~ 0.1 ms (FAISS inner-product search over ≤ 1 000 vectors).
    Cache-miss path: embedding (async thread) + pgvector search ≈ 20–80 ms.

    Args:
        vector_store: The knowledge-base vector store (``PgVectorStore`` in prod).
        embedding_provider: Shared embedding provider.
        cache: Semantic cache populated by the SlowThinker.
        metrics: Shared metrics instance.
        top_k: Context chunks to assemble per query.
        fallback_to_retrieval: When ``True`` (default), fall back to direct
            vector-store search on a cache miss instead of returning empty context.
    """

    def __init__(
        self,
        vector_store: PgVectorStore,
        embedding_provider: EmbeddingProvider,
        cache: SemanticCache,
        metrics: RagMetrics,
        top_k: int = 6,
        fallback_to_retrieval: bool = True,
    ) -> None:
        self._store = vector_store
        self._embeddings = embedding_provider
        self._cache = cache
        self._metrics = metrics
        self._top_k = top_k
        self._fallback = fallback_to_retrieval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_context(self, query: str) -> str:
        """Return formatted context string for the given user query.

        This is the primary entry-point called from ``MemoryRouter.get_context``.

        Args:
            query: The user's latest utterance.

        Returns:
            A formatted multi-chunk context string, or ``""`` if no relevant
            context was found.
        """
        self._metrics.total_queries += 1

        # 1. Embed the query
        q_emb = await self._embeddings.embed_single(query)

        # 2. Try the semantic cache (fast path)
        cached = await self._cache.get(q_emb, top_k=self._top_k)
        if cached:
            self._metrics.cache_hits += 1
            chunks = [entry.text for entry in cached]
            logger.debug(
                "FastTalker cache HIT: %d chunks for '%s…'", len(chunks), query[:40]
            )
            return _format_context(chunks)

        # 3. Cache miss — fall back to direct vector-store search
        self._metrics.cache_misses += 1
        logger.debug("FastTalker cache MISS for '%s…'", query[:40])

        if not self._fallback:
            return ""

        results = await self._store.search(q_emb, top_k=self._top_k)
        if not results:
            return ""

        # Populate cache with the retrieved chunks so the next similar query hits.
        # Use the query embedding as the cache key (document embeddings no longer
        # fetched from the DB).
        await self._cache.put_batch(
            [
                {
                    "query_embedding": q_emb,
                    "text": r.text,
                    "metadata": r.metadata,
                    "relevance_score": r.score,
                }
                for r in results
            ]
        )

        chunks = [r.text for r in results]
        logger.debug(
            "FastTalker fallback: %d chunks retrieved for '%s…'",
            len(chunks),
            query[:40],
        )
        return _format_context(chunks)


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------


def _format_context(chunks: List[str]) -> str:
    """Format retrieved chunks into an LLM-ready context string.

    The numbered format gives the LLM a clear structure and allows it to
    cite sources if needed.
    """
    if not chunks:
        return ""
    lines = [f"[{i + 1}] {chunk.strip()}" for i, chunk in enumerate(chunks)]
    return "\n\n".join(lines)
