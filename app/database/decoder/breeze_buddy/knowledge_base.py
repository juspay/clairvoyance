"""
Decoder functions for knowledge_base, kb_document and kb_chunk rows.
"""

import json
from typing import Any, Dict, List, Optional

import asyncpg

from app.schemas.breeze_buddy.knowledge_base import (
    EmbeddingConfig,
    KbDocument,
    KnowledgeBase,
    RetrievedChunk,
)


def _jsonb(value: Any) -> Dict[str, Any]:
    """Coerce a JSONB column to a dict.

    asyncpg returns JSONB as ``str`` unless a codec is registered on the
    pool (we don't register one), so every JSONB field in this module goes
    through here; also normalizes NULL to ``{}``.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return json.loads(value)


def decode_knowledge_base(row: asyncpg.Record) -> KnowledgeBase:
    """Decode a single knowledge_base row (optionally with count columns)."""
    embedding_config = _jsonb(row["embedding_config"])
    keys = row.keys()
    return KnowledgeBase(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_identifier"],
        name=row["name"],
        description=row["description"],
        embedding_config=(
            EmbeddingConfig(**embedding_config)
            if embedding_config
            else EmbeddingConfig()
        ),
        settings=_jsonb(row["settings"]),
        status=row["status"],
        document_count=row["document_count"] if "document_count" in keys else None,
        chunk_count=row["chunk_count"] if "chunk_count" in keys else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_knowledge_base_list(
    result: Optional[List[asyncpg.Record]],
) -> List[KnowledgeBase]:
    """Decode a list result into KnowledgeBase models ([] for empty/None).

    Standard list-shape wrapper used by accessor ``list_knowledge_bases``.
    """
    if not result:
        return []
    return [decode_knowledge_base(row) for row in result]


def decode_single_knowledge_base(
    result: Optional[List[asyncpg.Record]],
) -> Optional[KnowledgeBase]:
    """Decode the first row of a result, or None when nothing matched.

    Used by accessors whose query returns at most one row (get by id,
    create/update RETURNING).
    """
    if not result or len(result) == 0:
        return None
    return decode_knowledge_base(result[0])


def decode_kb_document(row: asyncpg.Record) -> KbDocument:
    """Decode one kb_document row into the KbDocument model.

    ``source_ref`` (GCS path / spreadsheet id) comes through the JSONB
    helper; status stays the raw enum string and is validated by Pydantic.
    """
    return KbDocument(
        id=str(row["id"]),
        kb_id=str(row["kb_id"]),
        source_type=row["source_type"],
        name=row["name"],
        source_ref=_jsonb(row["source_ref"]),
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        status=row["status"],
        error_message=row["error_message"],
        content_hash=row["content_hash"],
        chunk_count=row["chunk_count"],
        token_count=row["token_count"],
        synced_at=row["synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_kb_document_list(
    result: Optional[List[asyncpg.Record]],
) -> List[KbDocument]:
    """Decode a list result into KbDocument models ([] for empty/None).

    Used by document listing, the ingestion claim query and the sheets
    poller's due-documents query.
    """
    if not result:
        return []
    return [decode_kb_document(row) for row in result]


def decode_single_kb_document(
    result: Optional[List[asyncpg.Record]],
) -> Optional[KbDocument]:
    """Decode the first document row of a result, or None when absent.

    Used by get-by-id, resync and finish accessors (single-row queries).
    """
    if not result or len(result) == 0:
        return None
    return decode_kb_document(result[0])


def decode_retrieved_chunk(row: asyncpg.Record) -> RetrievedChunk:
    """Decode one hybrid-search hit into a RetrievedChunk.

    ``score`` is the RRF fusion score (only meaningful for ordering);
    ``vector_similarity`` is the cosine similarity of the vector leg and is
    None for keyword-only hits — retrieval's score_threshold deliberately
    skips those (exact SKU matches shouldn't be dropped for lacking a
    cosine score).
    """
    return RetrievedChunk(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        kb_id=str(row["kb_id"]),
        chunk_index=row["chunk_index"],
        text=row["text"],
        metadata=_jsonb(row["metadata"]),
        token_count=row["token_count"],
        score=float(row["score"]) if row["score"] is not None else 0.0,
        vector_similarity=(
            float(row["vector_similarity"])
            if row["vector_similarity"] is not None
            else None
        ),
        document_name=row["document_name"],
    )


def decode_retrieved_chunk_list(
    result: Optional[List[asyncpg.Record]],
) -> List[RetrievedChunk]:
    """Decode hybrid-search rows into RetrievedChunk list ([] for none).

    Terminal step of ``hybrid_search_chunks`` — everything the runtime
    (voice/chat/tool) and the /query hit-testing endpoint see comes
    through here.
    """
    if not result:
        return []
    return [decode_retrieved_chunk(row) for row in result]
