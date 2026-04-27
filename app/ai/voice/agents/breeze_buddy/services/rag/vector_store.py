"""
Vector store for the Breeze Buddy RAG pipeline.

``PgVectorStore`` is a lightweight stateless handle that delegates all search
to the ``rag_embeddings`` PostgreSQL table via pgvector's ``<=>`` cosine
distance operator.  There is no in-process state beyond the merchant/template
identifiers, so it is safe to share across async tasks or instantiate fresh
per call with zero cold-start latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from app.core.logger import logger


@dataclass
class SearchResult:
    """A single vector-search result."""

    text: str
    metadata: Dict[str, Any]
    score: float
    embedding: Optional[np.ndarray] = field(default=None, repr=False)


class PgVectorStore:
    """Stateless vector store backed by pgvector in PostgreSQL.

    Each ``search()`` call acquires a connection from the shared asyncpg pool
    and executes a cosine-similarity query via the ``<=>`` operator.

    Args:
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        dimension: Expected embedding dimension (default 1536).
    """

    def __init__(
        self,
        merchant_id: str,
        template_id: str,
        dimension: int = 1536,
    ) -> None:
        self._merchant_id = merchant_id
        self._template_id = template_id
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
    ) -> List[SearchResult]:
        """Cosine similarity search via pgvector.

        Args:
            query_embedding: 1-D float32 array of length ``dimension``.
            top_k: Maximum number of results.

        Returns:
            List of ``SearchResult`` sorted by similarity descending.
        """
        from app.database import get_pool
        from app.database.accessor.breeze_buddy.rag_embeddings import search_chunks

        async with get_pool().acquire() as conn:
            rows = await search_chunks(
                conn,
                self._merchant_id,
                self._template_id,
                query_embedding,
                top_k=top_k,
            )

        results = [
            SearchResult(
                text=row["text"],
                metadata=row["metadata"],
                score=row["score"],
            )
            for row in rows
        ]

        logger.debug(
            "PgVectorStore: %d results for %s/%s",
            len(results),
            self._merchant_id,
            self._template_id,
        )
        return results
