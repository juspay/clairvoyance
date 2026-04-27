"""
Knowledge-base index manager for Breeze Buddy RAG — pgvector edition.

Responsibilities
----------------
``build_knowledge_base``
    Download raw files from GCS → chunk → embed → upsert into
    ``rag_embeddings`` (PostgreSQL / pgvector).  Safe to call from the
    management API at any time; idempotent via ON CONFLICT upsert.

``get_pg_vector_store``
    Return a lightweight ``PgVectorStore`` handle for a given
    (merchant_id, template_id) pair.  Instantiation is instant — no I/O,
    no index build.  The store fetches embeddings on demand from pgvector.

``get_cached_index_stats``
    Query pgvector for live chunk/file counts and the last-indexed timestamp.
    Always accurate; no in-process cache.

``invalidate_index``
    Delete all rows for a (merchant_id, template_id) from ``rag_embeddings``.

There is intentionally **no** ``_INDEX_CACHE`` dict.  pgvector IS the store.
Each pod is stateless with respect to the knowledge base.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

import numpy as np

from app.ai.voice.agents.breeze_buddy.services.rag.embeddings import EmbeddingProvider
from app.ai.voice.agents.breeze_buddy.services.rag.gcs_loader import load_gcs_documents
from app.ai.voice.agents.breeze_buddy.services.rag.types import KnowledgeBaseConfig
from app.ai.voice.agents.breeze_buddy.services.rag.vector_store import PgVectorStore
from app.core.config.static import RAG_EMBEDDING_DIMENSION
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_knowledge_base(
    kb_config: KnowledgeBaseConfig,
    embedding_provider: EmbeddingProvider,
    merchant_id: str,
    template_id: str,
    gcs_bucket: str,
    gcs_prefix: str,
) -> int:
    """Download GCS documents, embed, and upsert into pgvector.

    This is the equivalent of the old ``force_rebuild_index`` but writes to
    PostgreSQL instead of building a FAISS index.

    Args:
        kb_config: Knowledge-base tuning config from the template.
        embedding_provider: Configured ``EmbeddingProvider`` instance.
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        gcs_bucket: GCS bucket name.
        gcs_prefix: GCS path prefix (``<merchant_id>/<template_id>/``).

    Returns:
        Number of chunks upserted.
    """
    from app.database import get_pool
    from app.database.accessor.breeze_buddy.rag_embeddings import upsert_knowledge_base

    logger.info(
        "IndexManager: building knowledge base for %s/%s from gs://%s/%s …",
        merchant_id,
        template_id,
        gcs_bucket,
        gcs_prefix,
    )
    t0 = time.time()

    # 1. Load + chunk documents from GCS
    chunks = await asyncio.to_thread(
        load_gcs_documents,
        gcs_bucket,
        gcs_prefix,
        kb_config.extensions,
        kb_config.chunk_size,
        kb_config.chunk_overlap,
    )

    if not chunks:
        logger.warning(
            "IndexManager: no documents found at gs://%s/%s — nothing indexed",
            gcs_bucket,
            gcs_prefix,
        )
        return 0

    texts = [c.text for c in chunks]
    metadata = [c.metadata for c in chunks]

    # 2. Embed all chunks in one call
    embeddings_array: np.ndarray = await embedding_provider.embed(texts)

    # 3. Upsert into pgvector — wrapped in a transaction so stale-chunk delete
    #    and the upsert are atomic (no window where old chunks can be served).
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            n = await upsert_knowledge_base(
                conn, merchant_id, template_id, texts, embeddings_array, metadata
            )

    elapsed = time.time() - t0
    logger.info(
        "IndexManager: upserted %d chunks for %s/%s in %.2f s",
        n,
        merchant_id,
        template_id,
        elapsed,
    )
    return n


def get_pg_vector_store(
    merchant_id: str,
    template_id: str,
    dimension: int = RAG_EMBEDDING_DIMENSION,
) -> PgVectorStore:
    """Return a ``PgVectorStore`` handle for the given template.

    Instantiation is instant — no database I/O.  The store fetches vectors
    from pgvector on each ``search()`` call.

    Args:
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        dimension: Embedding dimension (default: ``RAG_EMBEDDING_DIMENSION``).

    Returns:
        A ``PgVectorStore`` instance.
    """
    return PgVectorStore(
        merchant_id=merchant_id,
        template_id=template_id,
        dimension=dimension,
    )


async def get_cached_index_stats(
    merchant_id: str,
    template_id: str,
) -> Dict[str, Any]:
    """Query pgvector for live knowledge-base stats.

    Args:
        merchant_id: Merchant identifier.
        template_id: Template UUID.

    Returns:
        Dict with keys: ``chunk_count``, ``total_documents``,
        ``index_size_bytes``, ``last_indexed_at``, ``error_message``.
    """
    from app.database import get_pool
    from app.database.accessor.breeze_buddy.rag_embeddings import get_kb_stats

    try:
        async with get_pool().acquire() as conn:
            stats = await get_kb_stats(conn, merchant_id, template_id)

        last_indexed_at = stats.get("last_indexed_at")
        last_indexed_str = (
            last_indexed_at.isoformat() if last_indexed_at is not None else None
        )

        chunk_count = stats.get("chunk_count", 0)
        return {
            "chunk_count": chunk_count,
            "total_documents": stats.get("file_count", 0),
            # Approximate storage estimate: each vector is dimension * 4 bytes
            "index_size_bytes": chunk_count * RAG_EMBEDDING_DIMENSION * 4,
            "last_indexed_at": last_indexed_str,
            "error_message": None,
        }
    except Exception as exc:
        logger.warning(
            "IndexManager: failed to fetch stats for %s/%s: %s",
            merchant_id,
            template_id,
            exc,
        )
        return {
            "chunk_count": 0,
            "total_documents": 0,
            "index_size_bytes": 0,
            "last_indexed_at": None,
            "error_message": str(exc),
        }


async def invalidate_index(
    merchant_id: str,
    template_id: str,
) -> int:
    """Delete all chunks for a knowledge base from pgvector.

    Args:
        merchant_id: Merchant identifier.
        template_id: Template UUID.

    Returns:
        Number of rows deleted.
    """
    from app.database import get_pool
    from app.database.accessor.breeze_buddy.rag_embeddings import delete_knowledge_base

    async with get_pool().acquire() as conn:
        deleted = await delete_knowledge_base(conn, merchant_id, template_id)

    logger.info(
        "IndexManager: deleted %d chunks for %s/%s", deleted, merchant_id, template_id
    )
    return deleted
