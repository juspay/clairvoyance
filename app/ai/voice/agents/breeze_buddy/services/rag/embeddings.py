"""
Embedding provider for Breeze Buddy RAG.

Uses the Azure AI Foundry embedding endpoint (embed-v4.0, 1536-dim) via the
standard openai.OpenAI client (base_url + api_key pattern — NOT AzureOpenAI).

Credentials are read from:
    RAG_EMBEDDING_ENDPOINT  = "https://breeze-automatic.services.ai.azure.com/openai/v1/"
    RAG_EMBEDDING_API_KEY   = "<key>"
    RAG_EMBEDDING_DEPLOYMENT = "embed-v-4-0"

Design:
- Synchronous SDK call wrapped in asyncio.to_thread to avoid blocking the
  event loop.
- Batched in groups of 256 (configurable) for throughput.
- Empty strings replaced with "." to avoid API 422 errors.
- Input is always passed as a list (the API requires it).
"""

from __future__ import annotations

import asyncio
from typing import List

import numpy as np

from app.core.logger import logger

_DEFAULT_EMBEDDING_MODEL = "embed-v-4-0"
_DEFAULT_EMBEDDING_DIMENSION = 1536


class EmbeddingProvider:
    """Async embedding provider backed by Azure AI Foundry (OpenAI-compatible).

    Uses the standard ``openai.OpenAI`` client with ``base_url`` + ``api_key``.
    This is different from ``AzureOpenAI`` which uses ``azure_endpoint`` +
    ``api_version`` — the Foundry endpoint does not require an api_version.

    Args:
        api_key: API key for the embedding endpoint.
        endpoint: Base URL, e.g.
            ``"https://breeze-automatic.services.ai.azure.com/openai/v1/"``.
        deployment: Model/deployment name, e.g. ``"embed-v-4-0"``.
        dimension: Expected output dimension (1536 for embed-v4.0).
        max_batch_size: Maximum texts per single API call.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str,
        deployment: str = _DEFAULT_EMBEDDING_MODEL,
        dimension: int = _DEFAULT_EMBEDDING_DIMENSION,
        max_batch_size: int = 256,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package is required for EmbeddingProvider. "
                "Install via: uv add openai"
            ) from exc

        self._client = OpenAI(
            base_url=endpoint,
            api_key=api_key,
        )
        self._deployment = deployment
        self._dimension = dimension
        self._max_batch_size = max_batch_size

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Embedding dimensionality."""
        return self._dimension

    async def embed(self, texts: List[str]) -> np.ndarray:
        """Embed a batch of texts asynchronously.

        Args:
            texts: Non-empty list of strings.

        Returns:
            Float32 numpy array of shape ``(len(texts), dimension)``.
        """
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        all_embeddings: List[np.ndarray] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i : i + self._max_batch_size]
            batch_embeddings = await asyncio.to_thread(self._embed_sync, batch)
            all_embeddings.append(batch_embeddings)

        return np.vstack(all_embeddings).astype(np.float32)

    async def embed_single(self, text: str) -> np.ndarray:
        """Embed a single string.

        Args:
            text: Input string.

        Returns:
            Float32 numpy array of shape ``(dimension,)``.
        """
        result = await self.embed([text])
        return result[0]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _embed_sync(self, texts: List[str]) -> np.ndarray:
        """Synchronous embedding call — runs in a thread pool via asyncio.to_thread."""
        # API requires a list; replace empty strings to avoid 422 errors
        safe_texts = [t if t.strip() else "." for t in texts]
        response = self._client.embeddings.create(
            model=self._deployment,
            input=safe_texts,
        )
        vectors = [item.embedding for item in response.data]
        arr = np.array(vectors, dtype=np.float32)
        logger.debug(
            "EmbeddingProvider: embedded %d texts → shape %s", len(texts), arr.shape
        )
        return arr
