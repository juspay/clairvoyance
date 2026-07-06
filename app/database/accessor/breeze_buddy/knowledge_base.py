"""
Database accessor functions for knowledge base entities.
"""

from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import asyncpg

from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.knowledge_base import (
    decode_kb_document_list,
    decode_knowledge_base_list,
    decode_retrieved_chunk_list,
    decode_single_kb_document,
    decode_single_knowledge_base,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.knowledge_base import (
    claim_pending_kb_documents_query,
    delete_kb_chunks_beyond_index_query,
    delete_kb_document_query,
    delete_knowledge_base_query,
    find_templates_using_kb_query,
    finish_kb_document_query,
    get_chunk_hashes_for_document_query,
    get_kb_document_by_id_query,
    get_kb_full_text_query,
    get_kb_token_total_query,
    get_knowledge_base_by_id_query,
    get_merchant_chunk_total_query,
    get_sheet_documents_due_for_poll_query,
    hybrid_search_chunks_query,
    insert_kb_document_query,
    insert_knowledge_base_query,
    list_kb_documents_query,
    list_knowledge_bases_query,
    mark_kb_document_for_resync_query,
    requeue_stale_processing_documents_query,
    update_knowledge_base_query,
    upsert_kb_chunk_query,
)
from app.schemas.breeze_buddy.knowledge_base import (
    KbDocument,
    KnowledgeBase,
    RetrievedChunk,
)

# pgvector >= 0.8 supports iterative index scans, which fix HNSW returning
# fewer than k rows when the KB filter discards most candidates. Older
# pgvector rejects the GUC; probe once and remember.
_iterative_scan_supported: Optional[bool] = None


async def create_knowledge_base(
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    description: Optional[str],
    embedding_config: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[KnowledgeBase]:
    """Create a new knowledge base row and return it decoded.

    Called from ``create_kb_handler`` (POST /knowledge-bases). Re-raises
    ``asyncpg.UniqueViolationError`` untouched so the handler can map
    duplicate names to a 409.
    """
    logger.info(f"Creating knowledge base '{name}' for reseller {reseller_id}")
    try:
        query_text, values = insert_knowledge_base_query(
            id=str(uuid4()),
            reseller_id=reseller_id,
            merchant_identifier=merchant_id,
            name=name,
            description=description,
            embedding_config=embedding_config,
            settings=settings,
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_knowledge_base(result)
    except Exception as e:
        logger.error(f"Error creating knowledge base '{name}': {e}", exc_info=True)
        raise


async def get_knowledge_base_by_id(kb_id: str) -> Optional[KnowledgeBase]:
    """Get a knowledge base by ID (with live document/chunk counts).

    The most-shared read in the feature: API handlers (via ``_get_kb_or_404``),
    the ingestion worker (quota checks + embedding config) and retrieval
    (``_resolve_embedding_config`` on the query path) all come through here.
    """
    try:
        query_text, values = get_knowledge_base_by_id_query(kb_id)
        result = await run_parameterized_query(query_text, values)
        return decode_single_knowledge_base(result)
    except Exception as e:
        logger.error(f"Error getting knowledge base {kb_id}: {e}")
        raise


async def list_knowledge_bases(
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    include_archived: bool = False,
    search: Optional[str] = None,
    page: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[List[KnowledgeBase], int]:
    """List knowledge bases with RBAC filters. Returns (items, total).

    ``total`` is the unpaginated match count (window function) so the loom
    list page can paginate. Filters arrive pre-validated from
    ``accessible_scope_filters``; None means unrestricted (admin/wildcard).
    Called from ``list_kbs_handler`` (GET /knowledge-bases/list).
    """
    try:
        query_text, values = list_knowledge_bases_query(
            reseller_ids=reseller_ids,
            merchant_ids=merchant_ids,
            include_archived=include_archived,
            search=search,
            page=page,
            limit=limit,
        )
        result = await run_parameterized_query(query_text, values)
        total = result[0]["total_count"] if result else 0
        return decode_knowledge_base_list(result), total
    except Exception as e:
        logger.error(f"Error listing knowledge bases: {e}")
        raise


async def update_knowledge_base(
    kb_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
) -> Optional[KnowledgeBase]:
    """Update a knowledge base's metadata. Only provided fields change.

    Called from ``update_kb_handler`` (PUT /knowledge-bases/{id}) — name,
    description, settings and ACTIVE/ARCHIVED status. ``embedding_config``
    is deliberately not updatable: stored vectors were produced with it.
    """
    try:
        query_text, values = update_knowledge_base_query(
            kb_id, name=name, description=description, settings=settings, status=status
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_knowledge_base(result)
    except Exception as e:
        logger.error(f"Error updating knowledge base {kb_id}: {e}", exc_info=True)
        raise


async def delete_knowledge_base(kb_id: str) -> bool:
    """Delete a knowledge base (documents and chunks CASCADE).

    Called from ``delete_kb_handler`` only after the template in-use check
    and GCS cleanup have run; returns False when the row was already gone.
    """
    logger.info(f"Deleting knowledge base {kb_id}")
    try:
        query_text, values = delete_knowledge_base_query(kb_id)
        result = await run_parameterized_query(query_text, values)
        return bool(result)
    except Exception as e:
        logger.error(f"Error deleting knowledge base {kb_id}: {e}")
        raise


async def find_templates_using_kb(kb_id: str) -> List[Dict[str, Any]]:
    """Find templates whose configurations reference this KB (delete safety)."""
    try:
        query_text, values = find_templates_using_kb_query(kb_id)
        result = await run_parameterized_query(query_text, values)
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "reseller_id": row["reseller_id"],
                "merchant_id": row["merchant_id"],
            }
            for row in result
        ]
    except Exception as e:
        logger.error(f"Error finding templates using KB {kb_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


async def create_kb_document(
    kb_id: str,
    source_type: str,
    name: str,
    source_ref: Dict[str, Any],
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    content_hash: Optional[str] = None,
) -> Optional[KbDocument]:
    """Create a document in PENDING state (picked up by ingestion)."""
    try:
        query_text, values = insert_kb_document_query(
            id=str(uuid4()),
            kb_id=kb_id,
            source_type=source_type,
            name=name,
            source_ref=source_ref,
            mime_type=mime_type,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_kb_document(result)
    except Exception as e:
        logger.error(f"Error creating document '{name}' in KB {kb_id}: {e}")
        raise


async def get_kb_document_by_id(document_id: str) -> Optional[KbDocument]:
    """Get a document by ID.

    Used by the document delete / manual re-sync handlers (existence +
    ownership checks before acting).
    """
    try:
        query_text, values = get_kb_document_by_id_query(document_id)
        result = await run_parameterized_query(query_text, values)
        return decode_single_kb_document(result)
    except Exception as e:
        logger.error(f"Error getting document {document_id}: {e}")
        raise


async def list_kb_documents(kb_id: str) -> List[KbDocument]:
    """List all documents of a knowledge base, newest first.

    Serves the loom documents table (GET .../documents), the upload
    handler's per-KB document cap, and KB delete's GCS cleanup sweep.
    """
    try:
        query_text, values = list_kb_documents_query(kb_id)
        result = await run_parameterized_query(query_text, values)
        return decode_kb_document_list(result)
    except Exception as e:
        logger.error(f"Error listing documents for KB {kb_id}: {e}")
        raise


async def claim_pending_kb_documents(batch_size: int) -> List[KbDocument]:
    """Atomically claim PENDING documents for processing (race-safe)."""
    try:
        query_text, values = claim_pending_kb_documents_query(batch_size)
        result = await run_parameterized_query(query_text, values)
        return decode_kb_document_list(result)
    except Exception as e:
        logger.error(f"Error claiming pending documents: {e}")
        raise


async def requeue_stale_processing_documents(stale_minutes: int) -> int:
    """Requeue documents stuck in PROCESSING (crashed worker) back to PENDING.

    Self-healing sweep run at the start of every ingestion tick
    (``process_pending_documents``): if a pod died mid-ingest, its claimed
    documents become claimable again after ``stale_minutes``.
    """
    try:
        query_text, values = requeue_stale_processing_documents_query(stale_minutes)
        result = await run_parameterized_query(query_text, values)
        if result:
            logger.warning(f"Requeued {len(result)} stale PROCESSING documents")
        return len(result)
    except Exception as e:
        logger.error(f"Error requeuing stale documents: {e}")
        raise


async def mark_kb_document_for_resync(document_id: str) -> Optional[KbDocument]:
    """Send a READY/ERROR document back to PENDING for re-ingestion."""
    try:
        query_text, values = mark_kb_document_for_resync_query(document_id)
        result = await run_parameterized_query(query_text, values)
        return decode_single_kb_document(result)
    except Exception as e:
        logger.error(f"Error marking document {document_id} for resync: {e}")
        raise


async def finish_kb_document(
    document_id: str,
    status: str,
    error_message: Optional[str] = None,
    content_hash: Optional[str] = None,
    chunk_count: int = 0,
    token_count: int = 0,
) -> Optional[KbDocument]:
    """Finalize a document after an ingestion run (READY or ERROR).

    Called only from the ingestion worker's ``_process_document`` — both the
    success path (fresh counts, content hash) and the failure path (error
    message shown in the loom UI).
    """
    try:
        query_text, values = finish_kb_document_query(
            document_id, status, error_message, content_hash, chunk_count, token_count
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_kb_document(result)
    except Exception as e:
        logger.error(f"Error finalizing document {document_id}: {e}")
        raise


async def delete_kb_document(document_id: str) -> bool:
    """Delete a document (chunks CASCADE); False if it didn't exist.

    Called from ``delete_document_handler`` after RBAC; the handler also
    removes the GCS object and bumps the KB cache version.
    """
    try:
        query_text, values = delete_kb_document_query(document_id)
        result = await run_parameterized_query(query_text, values)
        return bool(result)
    except Exception as e:
        logger.error(f"Error deleting document {document_id}: {e}")
        raise


async def get_merchant_chunk_total(reseller_id: str) -> int:
    """Total chunks across a reseller's KBs.

    Feeds the per-reseller quota check in ingestion
    (``_enforce_chunk_quotas`` vs ``KB_MERCHANT_MAX_CHUNKS``) — the
    noisy-neighbor guard for shared-instance capacity.
    """
    try:
        query_text, values = get_merchant_chunk_total_query(reseller_id)
        result = await run_parameterized_query(query_text, values)
        return int(result[0]["total"]) if result else 0
    except Exception as e:
        logger.error(f"Error getting chunk total for reseller {reseller_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


async def get_chunk_hashes_for_document(document_id: str) -> Dict[int, str]:
    """Existing chunk_index -> content_hash map (incremental re-embedding)."""
    try:
        query_text, values = get_chunk_hashes_for_document_query(document_id)
        result = await run_parameterized_query(query_text, values)
        return {row["chunk_index"]: row["content_hash"] for row in result}
    except Exception as e:
        logger.error(f"Error getting chunk hashes for document {document_id}: {e}")
        raise


async def upsert_kb_chunk(
    document_id: str,
    kb_id: str,
    chunk_index: int,
    text: str,
    content_hash: str,
    embedding: List[float],
    metadata: Dict[str, Any],
    token_count: int,
) -> None:
    """Insert or replace one chunk at (document_id, chunk_index).

    Called from the ingestion embed loop for chunks whose content hash
    changed. Together with ``delete_kb_chunks_beyond_index`` this is the
    WRITE seam of the vector store (see ingestion.py module docstring).
    """
    try:
        query_text, values = upsert_kb_chunk_query(
            id=str(uuid4()),
            document_id=document_id,
            kb_id=kb_id,
            chunk_index=chunk_index,
            text_content=text,
            content_hash=content_hash,
            embedding=embedding,
            metadata=metadata,
            token_count=token_count,
        )
        await run_parameterized_query(query_text, values)
    except Exception as e:
        logger.error(
            f"Error upserting chunk {chunk_index} of document {document_id}: {e}"
        )
        raise


async def delete_kb_chunks_beyond_index(
    document_id: str, max_index_exclusive: int
) -> None:
    """Delete chunks past the new chunk list's end (document shrank)."""
    try:
        query_text, values = delete_kb_chunks_beyond_index_query(
            document_id, max_index_exclusive
        )
        await run_parameterized_query(query_text, values)
    except Exception as e:
        logger.error(f"Error trimming chunks for document {document_id}: {e}")
        raise


async def get_kb_full_text_rows(kb_ids: List[str]) -> List[Tuple[str, str]]:
    """All READY chunk texts (with document names) for full-injection mode."""
    try:
        query_text, values = get_kb_full_text_query(kb_ids)
        result = await run_parameterized_query(query_text, values)
        return [(row["document_name"], row["text"]) for row in result]
    except Exception as e:
        logger.error(f"Error getting full KB text for {kb_ids}: {e}")
        raise


async def get_kb_token_total(kb_ids: List[str]) -> int:
    """Total READY token count across KBs (AUTO mode size tiering)."""
    try:
        query_text, values = get_kb_token_total_query(kb_ids)
        result = await run_parameterized_query(query_text, values)
        return int(result[0]["total"]) if result else 0
    except Exception as e:
        logger.error(f"Error getting token total for KBs {kb_ids}: {e}")
        raise


async def hybrid_search_chunks(
    kb_ids: List[str],
    query_embedding: List[float],
    query_text: str,
    top_k: int,
    candidate_k: int = 50,
) -> List[RetrievedChunk]:
    """Run the single hybrid retrieval query (vector + FTS + trigram, RRF).

    Uses a dedicated connection so pgvector's iterative scan mode can be set
    with transaction scope (``SET LOCAL``) when available.
    """
    global _iterative_scan_supported
    sql, values = hybrid_search_chunks_query(
        kb_ids=kb_ids,
        query_embedding=query_embedding,
        query_text=query_text,
        top_k=top_k,
        candidate_k=candidate_k,
    )
    try:
        async for conn in get_db_connection():
            # Probe the pgvector >= 0.8 GUC once, in its OWN transaction so a
            # failed probe can't poison the query transaction. Only a missing
            # GUC pins the flag False; transient errors leave it None so the
            # next call re-probes instead of permanently degrading.
            if _iterative_scan_supported is None:
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "SET LOCAL hnsw.iterative_scan = 'relaxed_order'"
                        )
                    _iterative_scan_supported = True
                except asyncpg.UndefinedObjectError:
                    # SQLSTATE 42704 — the GUC doesn't exist. Typed catch is
                    # locale-independent (message-matching breaks under a
                    # non-English lc_messages and would re-probe every call).
                    _iterative_scan_supported = False
                    logger.info(
                        "pgvector iterative scan unavailable (pgvector < 0.8); "
                        "proceeding without"
                    )
                except Exception as probe_error:
                    logger.warning(
                        f"pgvector iterative-scan probe failed transiently, "
                        f"will re-probe: {probe_error}"
                    )

            # Single execution path: session GUCs are always applied together
            # regardless of the probe outcome.
            async with conn.transaction():
                if _iterative_scan_supported:
                    await conn.execute(
                        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'"
                    )
                # Loosen the trigram leg (default 0.6 misses moderate typos).
                await conn.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.3")
                rows = await conn.fetch(sql, *values)
                return decode_retrieved_chunk_list(rows)
        return []
    except Exception as e:
        logger.error(f"Error in hybrid search over KBs {kb_ids}: {e}")
        raise


async def get_sheet_documents_due_for_poll(
    min_sync_age_seconds: int,
) -> List[KbDocument]:
    """READY google_sheet documents older than the debounce floor."""
    try:
        query_text, values = get_sheet_documents_due_for_poll_query(
            min_sync_age_seconds
        )
        result = await run_parameterized_query(query_text, values)
        return decode_kb_document_list(result)
    except Exception as e:
        logger.error(f"Error listing sheet documents due for poll: {e}")
        raise
