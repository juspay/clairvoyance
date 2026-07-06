"""
Knowledge base ingestion worker.

Documents are claimed from PENDING via ``FOR UPDATE SKIP LOCKED`` so the
immediate post-upload kick and the scheduler sweeper (any pod) never process
the same document twice. Per document: connector.load -> chunk -> hash-diff
against existing chunks -> embed only changed chunks -> upsert/trim ->
READY/ERROR. Hash-diffing keys off (chunk_index, content_hash), so a 1000-row
sheet with 3 edited rows re-embeds 3 chunks.

FUTURE STORE SWAP: the ``upsert_kb_chunk`` / ``delete_kb_chunks_beyond_index``
pair below is the single WRITE seam for the vector store (READ seam:
retrieval.py ``retrieve()`` -- see the fuller migration note there). A second
backend (OpenSearch/Qdrant/...) plugs in by dual-writing here and backfilling
from the embeddings already stored in Postgres.
"""

import asyncio
import hashlib
from typing import List

from app.core.config.dynamic import (
    KB_INGEST_BATCH_SIZE,
    KB_MAX_CHUNKS_PER_KB,
    KB_MERCHANT_MAX_CHUNKS,
    KB_STALE_PROCESSING_MINUTES,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.knowledge_base import (
    claim_pending_kb_documents,
    delete_kb_chunks_beyond_index,
    finish_kb_document,
    get_chunk_hashes_for_document,
    get_knowledge_base_by_id,
    get_merchant_chunk_total,
    requeue_stale_processing_documents,
    upsert_kb_chunk,
)
from app.schemas.breeze_buddy.knowledge_base import (
    KbDocument,
    KbDocumentStatus,
    KnowledgeBase,
)
from app.services.embeddings import get_embedding_provider
from app.services.knowledge_base.chunking import build_chunks
from app.services.knowledge_base.connectors import get_connector
from app.services.knowledge_base.types import PreparedChunk
from app.services.redis import get_redis_service

# Conservative batching against embedding API limits (OpenAI: 2048 inputs,
# ~300K tokens per request for 3-small).
EMBED_BATCH_MAX_ITEMS = 128
EMBED_BATCH_MAX_TOKENS = 100_000


async def bump_kb_version(kb_id: str) -> None:
    """Invalidate version-keyed KB caches (full text / token totals).

    Cache keys embed a per-KB version counter, so INCR is the whole
    invalidation story -- no SCAN/pattern deletes.
    """
    try:
        redis = await get_redis_service()
        await redis.incr(f"kb:ver:{kb_id}")
    except Exception as e:
        logger.warning(f"Failed to bump KB version for {kb_id}: {e}")


# Strong refs to in-flight kicks: the event loop only holds weak references
# to tasks, so an unreferenced fire-and-forget task may be GC'd mid-run.
_kick_tasks: set = set()


def kick_ingestion() -> None:
    """Fire-and-forget processing kick after an upload/re-sync, so documents
    don't wait for the next scheduler tick. Race-safe via the claim query."""

    async def _run():
        # Wrapper so an ingestion failure inside a fire-and-forget task is
        # logged instead of becoming an unobserved task exception.
        try:
            await process_pending_documents()
        except Exception as e:
            logger.error(f"Ingestion kick failed: {e}", exc_info=True)

    task = asyncio.create_task(_run())
    _kick_tasks.add(task)
    task.add_done_callback(_kick_tasks.discard)


async def process_pending_documents() -> int:
    """Scheduler task + kick entry point. Returns processed document count."""
    stale_minutes = await KB_STALE_PROCESSING_MINUTES()
    try:
        await requeue_stale_processing_documents(stale_minutes)
    except Exception:
        pass  # already logged; claiming can proceed

    batch_size = await KB_INGEST_BATCH_SIZE()
    documents = await claim_pending_kb_documents(batch_size)
    if not documents:
        return 0

    logger.info(f"KB ingestion claimed {len(documents)} document(s)")
    for document in documents:
        await _process_document(document)
    return len(documents)


async def _process_document(document: KbDocument) -> None:
    """Run one claimed document through the full ingestion pipeline.

    connector.load -> build_chunks -> quota check -> hash-diff -> embed
    changed chunks (batched) -> upsert/trim -> READY + version bump.
    Any failure marks the document ERROR with the message the loom UI
    shows; errors never propagate to the scheduler loop, so one bad
    document can't stall the batch. Called from
    ``process_pending_documents`` for each claimed row.
    """
    logger.info(
        f"Ingesting document {document.id} ('{document.name}', "
        f"{document.source_type}) in KB {document.kb_id}"
    )
    try:
        kb = await get_knowledge_base_by_id(document.kb_id)
        if kb is None:
            raise ValueError(f"Knowledge base {document.kb_id} not found")

        connector = get_connector(document.source_type)
        content = await connector.load(document)
        chunks = build_chunks(content)

        if not chunks:
            raise ValueError("Document produced no chunks after parsing")

        await _enforce_chunk_quotas(kb, document, len(chunks))

        existing_hashes = await get_chunk_hashes_for_document(document.id)
        changed: List[tuple[int, PreparedChunk]] = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if existing_hashes.get(index) != chunk.content_hash
        ]

        embedded = 0
        for batch in _embedding_batches(changed):
            texts = [chunk.text for _, chunk in batch]
            provider = get_embedding_provider(kb.embedding_config)
            vectors = await provider.embed(texts, input_type="document")
            if len(vectors) != len(batch):
                # zip() would silently drop chunks and still mark the
                # document READY with missing embeddings.
                raise RuntimeError(
                    f"Embedding provider returned {len(vectors)} vectors "
                    f"for {len(batch)} inputs"
                )
            for (index, chunk), vector in zip(batch, vectors):
                await upsert_kb_chunk(
                    document_id=document.id,
                    kb_id=document.kb_id,
                    chunk_index=index,
                    text=chunk.text,
                    content_hash=chunk.content_hash,
                    embedding=vector,
                    metadata=chunk.metadata,
                    token_count=chunk.token_count,
                )
                embedded += 1

        await delete_kb_chunks_beyond_index(document.id, len(chunks))

        total_tokens = sum(chunk.token_count for chunk in chunks)
        content_hash = content.content_hash or _hash_chunks(chunks)
        await finish_kb_document(
            document.id,
            status=KbDocumentStatus.READY.value,
            content_hash=content_hash,
            chunk_count=len(chunks),
            token_count=total_tokens,
        )
        await bump_kb_version(document.kb_id)
        logger.info(
            f"Document {document.id} READY: {len(chunks)} chunks "
            f"({embedded} re-embedded, {len(chunks) - embedded} unchanged, "
            f"{total_tokens} tokens)"
        )

    except Exception as e:
        logger.error(f"Ingestion failed for document {document.id}: {e}", exc_info=True)
        try:
            await finish_kb_document(
                document.id,
                status=KbDocumentStatus.ERROR.value,
                error_message=str(e)[:2000],
                chunk_count=document.chunk_count,
                token_count=document.token_count,
            )
        except Exception as finish_error:
            logger.error(
                f"Failed to mark document {document.id} as ERROR: {finish_error}"
            )


async def _enforce_chunk_quotas(
    kb: KnowledgeBase, document: KbDocument, new_chunk_count: int
) -> None:
    """Noisy-neighbor protection: per-KB and per-reseller chunk caps.

    Both caps compare the store's CURRENT totals plus this document's delta,
    so they bound the knowledge base / reseller as a whole, not just the
    single document being ingested.
    """
    delta = new_chunk_count - document.chunk_count

    kb_cap = await KB_MAX_CHUNKS_PER_KB()
    kb_total = kb.chunk_count or 0
    if delta > 0 and kb_total + delta > kb_cap:
        raise ValueError(
            f"Knowledge base chunk cap exceeded ({kb_total} existing + {delta} "
            f"new > {kb_cap}); split content across knowledge bases or contact "
            "support to raise the limit"
        )

    merchant_cap = await KB_MERCHANT_MAX_CHUNKS()
    current_total = await get_merchant_chunk_total(kb.reseller_id)
    if delta > 0 and current_total + delta > merchant_cap:
        raise ValueError(
            f"Reseller chunk quota exceeded ({current_total} + {delta} > "
            f"{merchant_cap}); contact support to raise the limit"
        )


def _embedding_batches(changed: List[tuple[int, PreparedChunk]]):
    """Yield embed-API batches bounded by item count AND token budget.

    Two limits because providers enforce both (OpenAI: 2048 inputs,
    ~300K tokens per request); we stay well under with 128/100K. Used only
    by ``_process_document``'s embed loop.
    """
    batch: List[tuple[int, PreparedChunk]] = []
    batch_tokens = 0
    for item in changed:
        tokens = item[1].token_count
        if batch and (
            len(batch) >= EMBED_BATCH_MAX_ITEMS
            or batch_tokens + tokens > EMBED_BATCH_MAX_TOKENS
        ):
            yield batch
            batch, batch_tokens = [], 0
        batch.append(item)
        batch_tokens += tokens
    if batch:
        yield batch


def _hash_chunks(chunks: List[PreparedChunk]) -> str:
    """Whole-document fingerprint from the ordered chunk hashes.

    Fallback for connectors that don't provide a content hash of their own
    (files do via GCS bytes; this covers the rest). Stored on kb_document
    so ``detect_change`` / re-sync can cheaply answer "did anything change?".
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.content_hash.encode("utf-8"))
    return digest.hexdigest()
