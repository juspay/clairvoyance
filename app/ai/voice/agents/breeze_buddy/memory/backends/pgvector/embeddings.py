"""Azure OpenAI embeddings for memory dedup.

Best-effort: on failure, the caller stores the fact with embedding=None
and dedup falls back to exact/normalised text matching.
"""

from __future__ import annotations

from typing import List, Optional

from openai import AsyncAzureOpenAI

from app.core.config.static import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
)
from app.core.logger import logger

_EMBED_API_VERSION = "2024-02-01"

_client: Optional[AsyncAzureOpenAI] = None


def _get_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        _client = AsyncAzureOpenAI(
            api_key=AZURE_OPENAI_API_KEY,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version=_EMBED_API_VERSION,
        )
    return _client


async def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:
    """Embed a list of texts. Returns a parallel list; None on per-item failure."""
    if not texts:
        return []

    if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
        logger.warning(
            "[memory.embeddings] Azure OpenAI not configured; skipping embed"
        )
        return [None] * len(texts)

    if not AZURE_OPENAI_EMBEDDING_DEPLOYMENT:
        logger.warning(
            "[memory.embeddings] AZURE_OPENAI_EMBEDDING_DEPLOYMENT not set; skipping embed"
        )
        return [None] * len(texts)

    try:
        client = _get_client()
        response = await client.embeddings.create(
            model=AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
            input=texts,
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error(f"[memory.embeddings] embed_texts failed: {e}", exc_info=True)
        return [None] * len(texts)


async def embed_single(text: str) -> Optional[List[float]]:
    results = await embed_texts([text])
    return results[0] if results else None
