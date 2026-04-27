"""
Database accessor for the rag_embeddings table.

Provides:
  upsert_knowledge_base   — insert/replace all chunks for a template (re-index)
  search_chunks           — cosine similarity search via pgvector
  get_kb_stats            — chunk count, file count, last indexed timestamp
  delete_knowledge_base   — wipe all chunks for a template (invalidate)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np

from app.core.logger import logger
from app.database.queries.breeze_buddy.rag_embeddings import (
    DELETE_ALL_CHUNKS_QUERY,
    DELETE_STALE_CHUNKS_QUERY,
    GET_KB_STATS_QUERY,
    SEARCH_QUERY,
    UPSERT_CHUNKS_QUERY,
    upsert_chunks_args,
)


async def upsert_knowledge_base(
    conn: Any,
    merchant_id: str,
    template_id: str,
    texts: List[str],
    embeddings: np.ndarray,
    metadata: List[Dict[str, Any]],
) -> int:
    """Insert or replace all chunks for a knowledge base.

    Uses ``executemany`` with the upsert query so each chunk is an atomic
    INSERT … ON CONFLICT DO UPDATE.  After upserting, stale rows (chunk_index
    beyond the new count) are deleted so a shrinking knowledge base doesn't
    leave orphan rows.

    Args:
        conn: asyncpg connection (from pool.acquire()).
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        texts: Chunk text strings.
        embeddings: Float32 numpy array of shape (N, 1536).
        metadata: Per-chunk metadata dicts (must contain ``source`` key).

    Returns:
        Number of chunks upserted.
    """
    if not texts:
        return 0

    n = len(texts)
    chunks: List[Tuple[int, str, str, List[float]]] = []
    for i, (text, emb, meta) in enumerate(
        zip(texts, embeddings, metadata, strict=True)
    ):
        source = meta.get("source", "")
        chunks.append((i, source, text, emb.tolist()))

    rows = upsert_chunks_args(merchant_id, template_id, chunks)

    await conn.executemany(UPSERT_CHUNKS_QUERY, rows)

    # Remove any rows with chunk_index >= new count (handles shrinking KBs)
    await conn.execute(DELETE_STALE_CHUNKS_QUERY, merchant_id, template_id, n - 1)

    logger.info(
        "rag_embeddings: upserted %d chunks for %s/%s", n, merchant_id, template_id
    )
    return n


async def search_chunks(
    conn: Any,
    merchant_id: str,
    template_id: str,
    query_embedding: np.ndarray,
    top_k: int = 6,
) -> List[Dict[str, Any]]:
    """Cosine similarity search against rag_embeddings via pgvector.

    Args:
        conn: asyncpg connection.
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        query_embedding: 1-D float32 numpy array of length 1536.
        top_k: Maximum results to return.

    Returns:
        List of dicts with keys: text, metadata, score, embedding (np.ndarray).
        Sorted by score descending (highest similarity first).
    """
    emb_list = query_embedding.tolist()

    rows = await conn.fetch(
        SEARCH_QUERY,
        emb_list,
        merchant_id,
        template_id,
        top_k,
    )

    results = []
    for row in rows:
        results.append(
            {
                "text": row["chunk_text"],
                "metadata": {
                    "source": row["source_file"],
                    "chunk_index": row["chunk_index"],
                },
                "score": float(row["score"]),
            }
        )

    return results


async def get_kb_stats(
    conn: Any,
    merchant_id: str,
    template_id: str,
) -> Dict[str, Any]:
    """Return chunk count, file count, and last indexed timestamp.

    Args:
        conn: asyncpg connection.
        merchant_id: Merchant identifier.
        template_id: Template UUID.

    Returns:
        Dict with: chunk_count (int), file_count (int),
        last_indexed_at (datetime | None).
    """
    row = await conn.fetchrow(GET_KB_STATS_QUERY, merchant_id, template_id)

    return {
        "chunk_count": int(row["chunk_count"]) if row else 0,
        "file_count": int(row["file_count"]) if row else 0,
        "last_indexed_at": row["last_indexed_at"] if row else None,
    }


async def delete_knowledge_base(
    conn: Any,
    merchant_id: str,
    template_id: str,
) -> int:
    """Delete all chunks for a knowledge base.

    Args:
        conn: asyncpg connection.
        merchant_id: Merchant identifier.
        template_id: Template UUID.

    Returns:
        Number of rows deleted.
    """
    result = await conn.execute(DELETE_ALL_CHUNKS_QUERY, merchant_id, template_id)
    # result is a string like "DELETE 42"
    deleted = int(result.split()[-1]) if result else 0
    logger.info(
        "rag_embeddings: deleted %d chunks for %s/%s",
        deleted,
        merchant_id,
        template_id,
    )
    return deleted
