"""
Database query functions for knowledge_base, kb_document and kb_chunk tables.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

KNOWLEDGE_BASE_TABLE = "knowledge_base"
KB_DOCUMENT_TABLE = "kb_document"
KB_CHUNK_TABLE = "kb_chunk"


def _vector_literal(embedding: List[float]) -> str:
    """Serialize an embedding for a ``$N::halfvec(768)`` cast parameter.

    Embeddings are passed as pgvector text literals so no asyncpg type codec
    registration is needed on the pool.
    """
    return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"


# ---------------------------------------------------------------------------
# knowledge_base
# ---------------------------------------------------------------------------


def insert_knowledge_base_query(
    id: str,
    reseller_id: str,
    merchant_identifier: Optional[str],
    name: str,
    description: Optional[str],
    embedding_config: Dict[str, Any],
    settings: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Generate query to insert a knowledge base.

    ``RETURNING *`` lets the accessor decode the created row without a
    second round trip. Called from accessor ``create_knowledge_base``
    (POST /knowledge-bases). Duplicate names surface as a unique-violation
    from the partial indexes in migration 034 (409 at the API layer).
    """
    text = f"""
        INSERT INTO "{KNOWLEDGE_BASE_TABLE}"
        ("id", "reseller_id", "merchant_identifier", "name", "description",
         "embedding_config", "settings", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
        RETURNING *;
    """
    now = datetime.now()
    values = [
        id,
        reseller_id,
        merchant_identifier,
        name,
        description,
        json.dumps(embedding_config),
        json.dumps(settings),
        now,
        now,
    ]
    return text, values


def get_knowledge_base_by_id_query(kb_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get a knowledge base by ID with live counts."""
    text = f"""
        SELECT kb.*,
               (SELECT COUNT(*) FROM "{KB_DOCUMENT_TABLE}" d WHERE d."kb_id" = kb."id")
                   AS document_count,
               (SELECT COALESCE(SUM(d."chunk_count"), 0) FROM "{KB_DOCUMENT_TABLE}" d
                 WHERE d."kb_id" = kb."id") AS chunk_count
        FROM "{KNOWLEDGE_BASE_TABLE}" kb
        WHERE kb."id" = $1;
    """
    return text, [kb_id]


def list_knowledge_bases_query(
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    include_archived: bool = False,
    search: Optional[str] = None,
    page: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to list knowledge bases with RBAC filters + counts."""
    conditions: List[str] = []
    values: List[Any] = []
    param = 1

    if reseller_ids is not None:
        conditions.append(f'kb."reseller_id" = ANY(${param})')
        values.append(reseller_ids)
        param += 1

    if merchant_ids is not None:
        conditions.append(
            f'(kb."merchant_identifier" = ANY(${param}) OR kb."merchant_identifier" IS NULL)'
        )
        values.append(merchant_ids)
        param += 1

    if not include_archived:
        conditions.append("kb.\"status\" = 'ACTIVE'")

    if search:
        conditions.append(f'kb."name" ILIKE ${param}')
        values.append(f"%{search}%")
        param += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    pagination = ""
    if page is not None and limit is not None:
        pagination = f"LIMIT ${param} OFFSET ${param + 1}"
        values.extend([limit, (page - 1) * limit])
        param += 2

    text = f"""
        SELECT kb.*,
               COUNT(*) OVER() AS total_count,
               (SELECT COUNT(*) FROM "{KB_DOCUMENT_TABLE}" d WHERE d."kb_id" = kb."id")
                   AS document_count,
               (SELECT COALESCE(SUM(d."chunk_count"), 0) FROM "{KB_DOCUMENT_TABLE}" d
                 WHERE d."kb_id" = kb."id") AS chunk_count
        FROM "{KNOWLEDGE_BASE_TABLE}" kb
        {where}
        ORDER BY kb."updated_at" DESC
        {pagination};
    """
    return text, values


def update_knowledge_base_query(
    kb_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update a knowledge base. Only updates provided fields."""
    updates: List[str] = []
    values: List[Any] = []
    param = 1

    if name is not None:
        updates.append(f'"name" = ${param}')
        values.append(name)
        param += 1

    if description is not None:
        updates.append(f'"description" = ${param}')
        values.append(description)
        param += 1

    if settings is not None:
        updates.append(f'"settings" = ${param}::jsonb')
        values.append(json.dumps(settings))
        param += 1

    if status is not None:
        updates.append(f'"status" = ${param}')
        values.append(status)
        param += 1

    updates.append(f'"updated_at" = ${param}')
    values.append(datetime.now())
    param += 1

    values.append(kb_id)
    text = f"""
        UPDATE "{KNOWLEDGE_BASE_TABLE}"
        SET {', '.join(updates)}
        WHERE "id" = ${param}
        RETURNING *;
    """
    return text, values


def delete_knowledge_base_query(kb_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a knowledge base (chunks/documents CASCADE)."""
    text = f'DELETE FROM "{KNOWLEDGE_BASE_TABLE}" WHERE "id" = $1 RETURNING *;'
    return text, [kb_id]


def find_templates_using_kb_query(kb_id: str) -> Tuple[str, List[Any]]:
    """Generate query to find templates whose configurations reference a KB.

    Attachment lives at ``configurations.knowledge_base.knowledge_base_ids``
    (JSONB array of KB id strings) -- used for delete-safety checks.
    """
    text = """
        SELECT "id", "name", "reseller_id", "merchant_identifier"
        FROM "template"
        WHERE "configurations" -> 'knowledge_base' -> 'knowledge_base_ids' ? $1;
    """
    return text, [kb_id]


# ---------------------------------------------------------------------------
# kb_document
# ---------------------------------------------------------------------------


def insert_kb_document_query(
    id: str,
    kb_id: str,
    source_type: str,
    name: str,
    source_ref: Dict[str, Any],
    mime_type: Optional[str],
    size_bytes: Optional[int],
    content_hash: Optional[str],
) -> Tuple[str, List[Any]]:
    """Generate query to insert a document in PENDING state.

    PENDING is the hand-off contract with the ingestion worker: the row is
    invisible to retrieval until the worker flips it to READY. Called from
    accessor ``create_kb_document`` on file upload (and sheet connect in the
    sheets PR); the handler kicks ingestion right after.
    """
    text = f"""
        INSERT INTO "{KB_DOCUMENT_TABLE}"
        ("id", "kb_id", "source_type", "name", "source_ref", "mime_type",
         "size_bytes", "content_hash", "status", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, 'PENDING', $9, $10)
        RETURNING *;
    """
    now = datetime.now()
    values = [
        id,
        kb_id,
        source_type,
        name,
        json.dumps(source_ref),
        mime_type,
        size_bytes,
        content_hash,
        now,
        now,
    ]
    return text, values


def get_kb_document_by_id_query(document_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get a document by ID.

    Backs the document-scoped API operations (delete, manual re-sync) and
    the ingestion worker's KB lookups; plain primary-key fetch.
    """
    text = f'SELECT * FROM "{KB_DOCUMENT_TABLE}" WHERE "id" = $1;'
    return text, [document_id]


def list_kb_documents_query(kb_id: str) -> Tuple[str, List[Any]]:
    """Generate query to list all documents of a knowledge base.

    Newest first for the loom documents table; also used by the upload
    handler for the per-KB document-count cap and by KB delete for the
    best-effort GCS cleanup sweep.
    """
    text = f"""
        SELECT * FROM "{KB_DOCUMENT_TABLE}"
        WHERE "kb_id" = $1
        ORDER BY "created_at" DESC;
    """
    return text, [kb_id]


def claim_pending_kb_documents_query(batch_size: int) -> Tuple[str, List[Any]]:
    """Generate query to atomically claim PENDING documents for processing.

    ``FOR UPDATE SKIP LOCKED`` makes the claim race-safe when the immediate
    post-upload kick and the scheduler sweeper (possibly on another pod)
    overlap.
    """
    text = f"""
        UPDATE "{KB_DOCUMENT_TABLE}"
        SET "status" = 'PROCESSING', "updated_at" = now()
        WHERE "id" IN (
            SELECT "id" FROM "{KB_DOCUMENT_TABLE}"
            WHERE "status" = 'PENDING'
            ORDER BY "created_at"
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *;
    """
    return text, [batch_size]


def requeue_stale_processing_documents_query(
    stale_minutes: int,
) -> Tuple[str, List[Any]]:
    """Generate query to requeue documents stuck in PROCESSING (pod died)."""
    text = f"""
        UPDATE "{KB_DOCUMENT_TABLE}"
        SET "status" = 'PENDING', "updated_at" = now()
        WHERE "status" = 'PROCESSING'
          AND "updated_at" < now() - make_interval(mins => $1)
        RETURNING "id";
    """
    return text, [stale_minutes]


def mark_kb_document_for_resync_query(document_id: str) -> Tuple[str, List[Any]]:
    """Generate query to send a READY/ERROR document back to PENDING."""
    text = f"""
        UPDATE "{KB_DOCUMENT_TABLE}"
        SET "status" = 'PENDING', "error_message" = NULL, "updated_at" = now()
        WHERE "id" = $1 AND "status" IN ('READY', 'ERROR')
        RETURNING *;
    """
    return text, [document_id]


def finish_kb_document_query(
    document_id: str,
    status: str,
    error_message: Optional[str],
    content_hash: Optional[str],
    chunk_count: int,
    token_count: int,
) -> Tuple[str, List[Any]]:
    """Generate query to finalize a document after ingestion.

    Terminal state write of one ingestion run: READY with fresh counts, or
    ERROR with a message the loom UI surfaces. ``COALESCE`` keeps the last
    known content_hash when a failed run has none. Called only from the
    ingestion worker (``_process_document``).
    """
    text = f"""
        UPDATE "{KB_DOCUMENT_TABLE}"
        SET "status" = $2,
            "error_message" = $3,
            "content_hash" = COALESCE($4, "content_hash"),
            "chunk_count" = $5,
            "token_count" = $6,
            "synced_at" = now(),
            "updated_at" = now()
        WHERE "id" = $1
        RETURNING *;
    """
    return text, [
        document_id,
        status,
        error_message,
        content_hash,
        chunk_count,
        token_count,
    ]


def delete_kb_document_query(document_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a document (chunks CASCADE).

    Called from the DELETE document endpoint after RBAC; the handler also
    removes the GCS object and bumps the KB cache version afterwards.
    """
    text = f'DELETE FROM "{KB_DOCUMENT_TABLE}" WHERE "id" = $1 RETURNING *;'
    return text, [document_id]


def get_merchant_chunk_total_query(reseller_id: str) -> Tuple[str, List[Any]]:
    """Generate query for a reseller's total chunk count (quota enforcement)."""
    text = f"""
        SELECT COALESCE(SUM(d."chunk_count"), 0) AS total
        FROM "{KB_DOCUMENT_TABLE}" d
        JOIN "{KNOWLEDGE_BASE_TABLE}" kb ON kb."id" = d."kb_id"
        WHERE kb."reseller_id" = $1;
    """
    return text, [reseller_id]


# ---------------------------------------------------------------------------
# kb_chunk
# ---------------------------------------------------------------------------


def get_chunk_hashes_for_document_query(document_id: str) -> Tuple[str, List[Any]]:
    """Generate query for the (chunk_index, content_hash) map of a document."""
    text = f"""
        SELECT "chunk_index", "content_hash"
        FROM "{KB_CHUNK_TABLE}"
        WHERE "document_id" = $1;
    """
    return text, [document_id]


def upsert_kb_chunk_query(
    id: str,
    document_id: str,
    kb_id: str,
    chunk_index: int,
    text_content: str,
    content_hash: str,
    embedding: List[float],
    metadata: Dict[str, Any],
    token_count: int,
) -> Tuple[str, List[Any]]:
    """Generate query to insert or replace a chunk at (document_id, chunk_index)."""
    text = f"""
        INSERT INTO "{KB_CHUNK_TABLE}"
        ("id", "document_id", "kb_id", "chunk_index", "text", "content_hash",
         "embedding", "metadata", "token_count")
        VALUES ($1, $2, $3, $4, $5, $6, $7::halfvec(768), $8::jsonb, $9)
        ON CONFLICT ("document_id", "chunk_index") DO UPDATE SET
            "text" = EXCLUDED."text",
            "content_hash" = EXCLUDED."content_hash",
            "embedding" = EXCLUDED."embedding",
            "metadata" = EXCLUDED."metadata",
            "token_count" = EXCLUDED."token_count";
    """
    values = [
        id,
        document_id,
        kb_id,
        chunk_index,
        text_content,
        content_hash,
        _vector_literal(embedding),
        json.dumps(metadata),
        token_count,
    ]
    return text, values


def delete_kb_chunks_beyond_index_query(
    document_id: str, max_index_exclusive: int
) -> Tuple[str, List[Any]]:
    """Generate query to delete chunks past the new chunk list's end."""
    text = f"""
        DELETE FROM "{KB_CHUNK_TABLE}"
        WHERE "document_id" = $1 AND "chunk_index" >= $2;
    """
    return text, [document_id, max_index_exclusive]


def get_kb_full_text_query(kb_ids: List[str]) -> Tuple[str, List[Any]]:
    """Generate query for full-injection mode: all READY chunks in order."""
    text = f"""
        SELECT c."text", d."name" AS document_name
        FROM "{KB_CHUNK_TABLE}" c
        JOIN "{KB_DOCUMENT_TABLE}" d ON d."id" = c."document_id"
        WHERE c."kb_id" = ANY($1::uuid[]) AND d."status" = 'READY'
        ORDER BY d."created_at", c."document_id", c."chunk_index";
    """
    return text, [kb_ids]


def get_kb_token_total_query(kb_ids: List[str]) -> Tuple[str, List[Any]]:
    """Generate query for the total READY token count across KBs.

    Sums document-level counters (cheap) rather than scanning chunks.
    Drives AUTO-mode size tiering (full-injection vs auto-retrieve) and the
    full-injection token cap; called via retrieval ``get_kb_token_count``.
    """
    text = f"""
        SELECT COALESCE(SUM("token_count"), 0) AS total
        FROM "{KB_DOCUMENT_TABLE}"
        WHERE "kb_id" = ANY($1::uuid[]) AND "status" = 'READY';
    """
    return text, [kb_ids]


def hybrid_search_chunks_query(
    kb_ids: List[str],
    query_embedding: List[float],
    query_text: str,
    top_k: int,
    candidate_k: int = 50,
    rrf_k: int = 60,
) -> Tuple[str, List[Any]]:
    """Generate the single hybrid retrieval query.

    Three legs over the KB-filtered chunk set -- vector KNN (HNSW), full-text
    ('simple' config, websearch syntax) and trigram word-similarity (typo
    tolerance for SKUs/product names) -- fused with Reciprocal Rank Fusion:
    ``score = sum(1 / (rrf_k + rank_leg))``.
    """
    text = f"""
        WITH vec AS (
            SELECT "id",
                   ROW_NUMBER() OVER (ORDER BY "embedding" <=> $2::halfvec(768)) AS rank,
                   1 - ("embedding" <=> $2::halfvec(768)) AS similarity
            FROM "{KB_CHUNK_TABLE}"
            WHERE "kb_id" = ANY($1::uuid[]) AND "embedding" IS NOT NULL
            ORDER BY "embedding" <=> $2::halfvec(768)
            LIMIT $4
        ),
        fts AS (
            SELECT "id",
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd("tsv", websearch_to_tsquery('simple', $3)) DESC
                   ) AS rank
            FROM "{KB_CHUNK_TABLE}"
            WHERE "kb_id" = ANY($1::uuid[])
              AND "tsv" @@ websearch_to_tsquery('simple', $3)
            ORDER BY rank
            LIMIT $4
        ),
        trgm AS (
            SELECT "id",
                   ROW_NUMBER() OVER (
                       ORDER BY word_similarity($3, "text") DESC
                   ) AS rank
            FROM "{KB_CHUNK_TABLE}"
            WHERE "kb_id" = ANY($1::uuid[])
              AND "text" %> $3
            ORDER BY rank
            LIMIT $4
        )
        SELECT c."id", c."document_id", c."kb_id", c."chunk_index", c."text",
               c."metadata", c."token_count",
               d."name" AS document_name,
               v."similarity" AS vector_similarity,
               COALESCE(1.0 / ($5 + v.rank), 0)
                   + COALESCE(1.0 / ($5 + f.rank), 0)
                   + COALESCE(1.0 / ($5 + t.rank), 0) AS score
        FROM "{KB_CHUNK_TABLE}" c
        -- READY-only: never serve chunks of documents that are mid-resync
        -- (PROCESSING) or half-ingested (ERROR); mirrors get_kb_full_text_query.
        JOIN "{KB_DOCUMENT_TABLE}" d
            ON d."id" = c."document_id" AND d."status" = 'READY'
        LEFT JOIN vec v ON v."id" = c."id"
        LEFT JOIN fts f ON f."id" = c."id"
        LEFT JOIN trgm t ON t."id" = c."id"
        WHERE v."id" IS NOT NULL OR f."id" IS NOT NULL OR t."id" IS NOT NULL
        ORDER BY score DESC
        LIMIT $6;
    """
    values = [
        kb_ids,
        _vector_literal(query_embedding),
        query_text,
        candidate_k,
        rrf_k,
        top_k,
    ]
    return text, values
