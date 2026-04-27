"""
Pydantic types for the Breeze Buddy RAG service.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class KnowledgeBaseConfig(BaseModel):
    """Template-level knowledge-base configuration.

    Stored under ``configurations.knowledge_base`` in the template JSON.
    Knowledge files are stored in GCS at:
      ``gs://<RAG_GCS_BUCKET>/<merchant_id>/<template_id>/``

    The bucket is set globally via the ``RAG_GCS_BUCKET`` env var.
    The path is derived automatically from the template — no manual config needed.

    Example — enable RAG with defaults::

        { "knowledge_base": {} }

    Example — with custom chunking::

        {
            "knowledge_base": {
                "chunk_size": 400,
                "chunk_overlap": 40,
                "top_k": 5
            }
        }
    """

    # Supported file extensions to ingest
    extensions: List[str] = Field(
        default=[".txt", ".md", ".rst", ".text", ".pdf"],
        description="File extensions to ingest from GCS.",
    )

    # Chunking
    chunk_size: int = Field(
        512,
        ge=64,
        le=4096,
        description="Maximum characters per document chunk.",
    )
    chunk_overlap: int = Field(
        64,
        ge=0,
        le=512,
        description="Overlap characters between adjacent chunks.",
    )

    # Retrieval
    top_k: int = Field(
        6,
        ge=1,
        le=20,
        description="Number of chunks to retrieve per query.",
    )
    prefetch_top_k: int = Field(
        10,
        ge=1,
        le=40,
        description="Number of chunks to prefetch per Slow Thinker prediction.",
    )

    # Cache
    cache_max_size: int = Field(
        1000,
        ge=100,
        description="Maximum number of entries in the semantic cache.",
    )
    cache_ttl_seconds: float = Field(
        300.0,
        ge=30.0,
        description="Time-to-live for cache entries in seconds.",
    )
    cache_similarity_threshold: float = Field(
        0.40,
        ge=0.0,
        le=1.0,
        description="Cosine-similarity threshold for a cache hit.",
    )

    # Slow Thinker
    max_predictions: int = Field(
        4,
        ge=1,
        le=10,
        description="Number of follow-up topics for the Slow Thinker to predict per turn.",
    )
    slow_thinker_rate_limit: float = Field(
        0.5,
        ge=0.0,
        description="Minimum seconds between Slow Thinker prediction cycles.",
    )

    # LLM provider for predictions (falls back to Azure if not set)
    prediction_llm_api_key: Optional[str] = Field(
        None,
        description="Azure OpenAI API key used for Slow Thinker predictions. "
        "Defaults to the global AZURE_OPENAI_API_KEY env var.",
    )
    prediction_llm_endpoint: Optional[str] = Field(
        None,
        description="Azure OpenAI endpoint used for Slow Thinker predictions. "
        "Defaults to the global AZURE_OPENAI_ENDPOINT env var.",
    )
    prediction_llm_model: Optional[str] = Field(
        None,
        description="Azure OpenAI deployment name for predictions. "
        "Defaults to AZURE_BREEZE_BUDDY_OPENAI_MODEL.",
    )


class DocumentChunk(BaseModel):
    """A chunk of a document ready for embedding."""

    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RagMetrics(BaseModel):
    """Aggregate runtime metrics for one MemoryRouter instance."""

    cache_hits: int = 0
    cache_misses: int = 0
    prefetch_operations: int = 0
    predictions_made: int = 0
    total_queries: int = 0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0
