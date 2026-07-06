"""
Knowledge base retrieval service -- the latency-critical path.

``retrieve()`` = query embedding (Redis-cached) + one hybrid SQL round trip
(vector KNN + full-text + trigram, RRF-fused). Callers on the voice hot path
wrap it in ``asyncio.wait_for`` and fail open. Full-KB text / token totals
(for full-injection mode) are cached in Redis under version-stamped keys;
ingestion bumps the version on every publish, so no pattern invalidation is
needed.

FUTURE STORE SWAP (OpenSearch/Qdrant/Turbopuffer): ``retrieve()`` is the
single READ seam -- swapping the vector store means reimplementing only the
``hybrid_search_chunks`` call it delegates to (the WRITE seam is the chunk
upsert/trim pair in ingestion.py). Deliberately NOT abstracted into a
VectorStore interface while pgvector is the only backend; when a second
backend lands, extract the interface then, dual-write, backfill by copying
stored embeddings (no re-embed needed), and flip reads per-merchant via
dynamic config. Postgres stays the system of record for KB/document
metadata regardless of backend. Triggers to revisit: ~5M total chunks,
p95 retrieval drift (HNSW outgrowing RAM), or vernacular keyword quality
needing real language analyzers.
"""

import hashlib
import json
import time
from typing import List, Optional, Tuple

from app.core.config.dynamic import KB_EMBED_CACHE_TTL_SECONDS
from app.core.logger import logger
from app.database.accessor.breeze_buddy.knowledge_base import (
    get_kb_full_text_rows,
    get_kb_token_total,
    get_knowledge_base_by_id,
    hybrid_search_chunks,
)
from app.schemas.breeze_buddy.knowledge_base import EmbeddingConfig, RetrievedChunk
from app.services.embeddings import get_embedding_provider
from app.services.redis import get_redis_service

_FULL_TEXT_CACHE_TTL_SECONDS = 3600
_TOKEN_TOTAL_CACHE_TTL_SECONDS = 3600


def _normalize_query(query: str) -> str:
    """Collapse whitespace so trivially-different phrasings share one
    embedding cache entry (and one consistent FTS/trigram input)."""
    return " ".join(query.split()).strip()


async def _get_kb_versions(kb_ids: List[str]) -> List[str]:
    """Per-KB version counters (bumped by ingestion) for cache keys."""
    versions = []
    try:
        redis = await get_redis_service()
        for kb_id in sorted(kb_ids):
            version = await redis.get(f"kb:ver:{kb_id}")
            versions.append(version or "0")
    except Exception:
        # Redis down -> unique key per call (cache miss, correct results).
        versions = [str(time.time())]
    return versions


def _versioned_key(prefix: str, kb_ids: List[str], versions: List[str]) -> str:
    """Build a Redis cache key that embeds the KB version counters.

    Because the version is part of the key, ingestion invalidates caches by
    just INCR-ing the counter — old keys become unreachable and expire via
    TTL; no SCAN/pattern deletes. Used by the full-text and token-total
    caches below.
    """
    raw = "|".join(sorted(kb_ids)) + "@" + ".".join(versions)
    return f"{prefix}:{hashlib.sha1(raw.encode()).hexdigest()}"


async def _resolve_embedding_config(kb_ids: List[str]) -> EmbeddingConfig:
    """Query embedding uses the first KB's provider. Cross-provider KB
    attachment is unsupported (vectors aren't comparable across providers);
    template validation enforces this at attach time."""
    kb = await get_knowledge_base_by_id(kb_ids[0])
    if kb is None:
        raise ValueError(f"Knowledge base {kb_ids[0]} not found")
    return kb.embedding_config


async def _embed_query_cached(query: str, config: EmbeddingConfig) -> List[float]:
    """Embed the query with a Redis cache in front (dodges the fat p95 tail
    of embedding APIs for repeated phrasings)."""
    normalized = _normalize_query(query)
    cache_key = (
        f"kb:emb:{config.provider}:{config.model}:"
        f"{hashlib.sha1(normalized.lower().encode()).hexdigest()}"
    )

    redis = None
    try:
        redis = await get_redis_service()
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass  # cache is best-effort

    provider = get_embedding_provider(config)
    vectors = await provider.embed([normalized], input_type="query")
    vector = vectors[0]

    if redis is not None:
        try:
            ttl = await KB_EMBED_CACHE_TTL_SECONDS()
            await redis.setex(cache_key, json.dumps(vector), ttl)
        except Exception:
            pass
    return vector


async def retrieve(
    kb_ids: List[str],
    query: str,
    top_k: int = 6,
    score_threshold: float = 0.0,
) -> List[RetrievedChunk]:
    """Hybrid retrieval over the given knowledge bases.

    Raises on failure -- callers own the timeout/fail-open policy
    (``asyncio.wait_for`` on the voice path, looser budgets in chat/tool).
    """
    if not kb_ids:
        return []
    normalized = _normalize_query(query)
    if not normalized:
        return []

    started = time.perf_counter()
    config = await _resolve_embedding_config(kb_ids)
    query_embedding = await _embed_query_cached(normalized, config)
    embed_ms = (time.perf_counter() - started) * 1000

    chunks = await hybrid_search_chunks(
        kb_ids=kb_ids,
        query_embedding=query_embedding,
        query_text=normalized,
        top_k=top_k,
    )

    if score_threshold > 0:
        # Threshold applies to the vector leg; keyword-only hits (exact
        # SKU/price matches with no meaningful cosine score) are kept.
        chunks = [
            chunk
            for chunk in chunks
            if chunk.vector_similarity is None
            or chunk.vector_similarity >= score_threshold
        ]

    total_ms = (time.perf_counter() - started) * 1000
    logger.info(
        f"KB retrieve: {len(chunks)} chunks from {len(kb_ids)} KB(s) in "
        f"{total_ms:.0f}ms (embed {embed_ms:.0f}ms)"
    )
    return chunks


async def get_kb_token_count(kb_ids: List[str]) -> int:
    """Total READY tokens across KBs (AUTO mode size tiering), cached."""
    if not kb_ids:
        return 0
    versions = await _get_kb_versions(kb_ids)
    cache_key = _versioned_key("kb:tokens", kb_ids, versions)
    try:
        redis = await get_redis_service()
        cached = await redis.get(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        redis = None

    total = await get_kb_token_total(kb_ids)
    if redis is not None:
        try:
            await redis.setex(cache_key, str(total), _TOKEN_TOTAL_CACHE_TTL_SECONDS)
        except Exception:
            pass
    return total


async def get_full_kb_text(
    kb_ids: List[str], max_tokens: Optional[int] = None
) -> Tuple[str, int]:
    """Concatenated READY chunk text for full-injection mode, cached.

    Returns (text, token_count). ``max_tokens`` guards against a KB that
    grew past the injection threshold between resolution and fetch.
    """
    if not kb_ids:
        return "", 0
    versions = await _get_kb_versions(kb_ids)
    cache_key = _versioned_key("kb:text", kb_ids, versions)

    redis = None
    try:
        redis = await get_redis_service()
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = json.loads(cached)
            return payload["text"], payload["tokens"]
    except Exception:
        redis = None

    rows = await get_kb_full_text_rows(kb_ids)
    sections: List[str] = []
    current_doc = None
    for document_name, chunk_text in rows:
        if document_name != current_doc:
            sections.append(f"\n### {document_name}\n")
            current_doc = document_name
        sections.append(chunk_text)
    text = "\n".join(sections).strip()
    tokens = await get_kb_token_count(kb_ids)

    if max_tokens is not None and tokens > max_tokens:
        logger.warning(
            f"Full KB text for {kb_ids} is {tokens} tokens, over the "
            f"{max_tokens} cap -- refusing full injection"
        )
        return "", tokens

    if redis is not None:
        try:
            await redis.setex(
                cache_key,
                json.dumps({"text": text, "tokens": tokens}),
                _FULL_TEXT_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass
    return text, tokens
