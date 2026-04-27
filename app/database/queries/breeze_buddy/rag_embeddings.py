"""
SQL query builders for the rag_embeddings table.

All queries use $N positional parameters (asyncpg style).
The ``embedding`` column is ``vector(1536)`` — values are passed as Python
lists of floats thanks to the codec registered in ``app/database/__init__.py``.
"""

from __future__ import annotations

from typing import Any, List, Tuple

# ---------------------------------------------------------------------------
# Upsert (insert or replace on chunk_index conflict)
# ---------------------------------------------------------------------------

UPSERT_CHUNKS_QUERY = """
INSERT INTO rag_embeddings
    (merchant_id, template_id, chunk_index, source_file, chunk_text, embedding, indexed_at)
VALUES
    ($1, $2, $3, $4, $5, $6::vector, now())
ON CONFLICT (merchant_id, template_id, chunk_index)
DO UPDATE SET
    source_file = EXCLUDED.source_file,
    chunk_text  = EXCLUDED.chunk_text,
    embedding   = EXCLUDED.embedding,
    indexed_at  = now()
"""


def upsert_chunks_args(
    merchant_id: str,
    template_id: str,
    chunks: List[Tuple[int, str, str, List[float]]],
) -> List[Tuple[Any, ...]]:
    """Return a list of row tuples ready for ``executemany``.

    Args:
        merchant_id: Merchant identifier.
        template_id: Template UUID.
        chunks: List of (chunk_index, source_file, chunk_text, embedding_list).

    Returns:
        List of 6-tuples: (merchant_id, template_id, chunk_index, source_file,
        chunk_text, embedding).
    """
    return [
        (merchant_id, template_id, idx, source, text, emb)
        for idx, source, text, emb in chunks
    ]


# ---------------------------------------------------------------------------
# Delete stale chunks after a re-index
# Removes any rows whose chunk_index is beyond the new chunk count.
# ---------------------------------------------------------------------------

DELETE_STALE_CHUNKS_QUERY = """
DELETE FROM rag_embeddings
WHERE merchant_id = $1
  AND template_id = $2
  AND chunk_index > $3
"""


# ---------------------------------------------------------------------------
# Similarity search
# ---------------------------------------------------------------------------

SEARCH_QUERY = """
SELECT
    chunk_text,
    source_file,
    chunk_index,
    1 - (embedding <=> $1::vector) AS score
FROM rag_embeddings
WHERE merchant_id = $2
  AND template_id = $3
ORDER BY embedding <=> $1::vector
LIMIT $4
"""


# ---------------------------------------------------------------------------
# Status / metadata queries — single round-trip
# ---------------------------------------------------------------------------

GET_KB_STATS_QUERY = """
SELECT
    COUNT(*)                    AS chunk_count,
    COUNT(DISTINCT source_file) AS file_count,
    MAX(indexed_at)             AS last_indexed_at
FROM rag_embeddings
WHERE merchant_id = $1
  AND template_id = $2
"""


# ---------------------------------------------------------------------------
# Delete all chunks for a knowledge base (used by invalidate endpoint)
# ---------------------------------------------------------------------------

DELETE_ALL_CHUNKS_QUERY = """
DELETE FROM rag_embeddings
WHERE merchant_id = $1
  AND template_id = $2
"""
