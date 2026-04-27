"""
RagMemoryRouter – central orchestrator for the Breeze Buddy RAG service.

One ``RagMemoryRouter`` is created per voice-call.  It wires together:

  - ``EmbeddingProvider``  – embeddings via Azure AI Foundry
  - ``PgVectorStore``      – stateless handle to the pgvector knowledge base
                             (no in-process FAISS index; safe across pods)
  - ``SemanticCache``      – in-memory FAISS semantic cache per call
  - ``SlowThinker``        – background prefetch agent (asyncio task)
  - ``FastTalker``         – foreground sub-ms context retrieval

Knowledge files are stored in GCS at::

    gs://<RAG_GCS_BUCKET>/<merchant_id>/<template_id>/

The bucket is read from ``RAG_GCS_BUCKET`` env var; the path is derived
automatically from the template — no manual configuration is required.

Usage from the Breeze Buddy pipeline::

    from app.ai.voice.agents.breeze_buddy.services.rag import RagMemoryRouter

    router = await RagMemoryRouter.build(
        kb_config=template.configurations.knowledge_base,
        merchant_id=template.merchant_id,
        template_id=template.id,
    )
    await router.start()

    # Inside the LLM context-assembly callback:
    context = await router.get_context(user_utterance, conversation_history)

    await router.stop()
"""

from __future__ import annotations

from typing import Optional

from app.ai.voice.agents.breeze_buddy.services.rag.embeddings import (
    EmbeddingProvider,
)
from app.ai.voice.agents.breeze_buddy.services.rag.fast_talker import FastTalker
from app.ai.voice.agents.breeze_buddy.services.rag.index_manager import (
    get_pg_vector_store,
)
from app.ai.voice.agents.breeze_buddy.services.rag.semantic_cache import SemanticCache
from app.ai.voice.agents.breeze_buddy.services.rag.slow_thinker import SlowThinker
from app.ai.voice.agents.breeze_buddy.services.rag.types import (
    KnowledgeBaseConfig,
    RagMetrics,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    RAG_EMBEDDING_API_KEY,
    RAG_EMBEDDING_DEPLOYMENT,
    RAG_EMBEDDING_DIMENSION,
    RAG_EMBEDDING_ENDPOINT,
    RAG_GCS_BUCKET,
)
from app.core.logger import logger


class RagMemoryRouter:
    """Voice-optimised dual-agent RAG orchestrator.

    Do not construct directly – use ``RagMemoryRouter.build()``.
    """

    def __init__(
        self,
        fast_talker: FastTalker,
        slow_thinker: SlowThinker,
        cache: SemanticCache,
        metrics: RagMetrics,
        kb_config: KnowledgeBaseConfig,
        merchant_id: str,
        template_id: str,
    ) -> None:
        self._fast_talker = fast_talker
        self._slow_thinker = slow_thinker
        self._cache = cache
        self._metrics = metrics
        self._kb_config = kb_config
        self._merchant_id = merchant_id
        self._template_id = template_id
        self._conversation_history: list[str] = []

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def build(
        cls,
        kb_config: KnowledgeBaseConfig,
        merchant_id: str,
        template_id: str,
        azure_api_key: Optional[str] = None,
        azure_endpoint: Optional[str] = None,
        azure_embedding_deployment: str = "text-embedding-3-small",
        azure_prediction_deployment: Optional[str] = None,
        azure_api_version: str = "2024-02-01",
    ) -> "RagMemoryRouter":
        """Build a ``RagMemoryRouter`` for a specific template.

        The PgVectorStore is instantiated instantly (no I/O).  Embedding
        credentials are read from ``RAG_EMBEDDING_*`` env vars.  The LLM
        used by SlowThinker for follow-up prediction still uses the main
        Azure OpenAI credentials (``AZURE_OPENAI_*``).

        Args:
            kb_config: Knowledge-base tuning config from the template.
            merchant_id: Merchant identifier (from ``TemplateModel.merchant_id``).
            template_id: Template UUID (from ``TemplateModel.id``).
            azure_api_key: Unused — kept for backward-compat signature only.
            azure_endpoint: Unused — kept for backward-compat signature only.
            azure_embedding_deployment: Unused — overridden by RAG_EMBEDDING_DEPLOYMENT.
            azure_prediction_deployment: Azure deployment for Slow Thinker LLM.
            azure_api_version: Azure OpenAI REST API version for Slow Thinker LLM.

        Returns:
            A fully wired ``RagMemoryRouter`` (not yet started).
        """
        # Embedding provider — uses dedicated RAG_EMBEDDING_* credentials
        emb_endpoint = RAG_EMBEDDING_ENDPOINT
        emb_api_key = RAG_EMBEDDING_API_KEY
        emb_deployment = RAG_EMBEDDING_DEPLOYMENT
        emb_dimension = RAG_EMBEDDING_DIMENSION

        if not emb_endpoint or not emb_api_key:
            raise ValueError(
                "RAG_EMBEDDING_ENDPOINT and RAG_EMBEDDING_API_KEY env vars are required."
            )

        embedding_provider = EmbeddingProvider(
            api_key=emb_api_key,
            endpoint=emb_endpoint,
            deployment=emb_deployment,
            dimension=emb_dimension,
        )

        # SlowThinker LLM — still uses main Azure OpenAI credentials
        pred_api_key = (
            kb_config.prediction_llm_api_key or azure_api_key or AZURE_OPENAI_API_KEY
        ) or None
        pred_endpoint = (
            kb_config.prediction_llm_endpoint or azure_endpoint or AZURE_OPENAI_ENDPOINT
        ) or None
        pred_deployment = (
            kb_config.prediction_llm_model
            or azure_prediction_deployment
            or AZURE_BREEZE_BUDDY_OPENAI_MODEL
        )

        # PgVectorStore — stateless, instant, no index build needed
        vector_store = get_pg_vector_store(
            merchant_id=merchant_id,
            template_id=template_id,
            dimension=emb_dimension,
        )

        # Per-call semantic cache (in-process FAISS, ephemeral)
        cache = SemanticCache(
            dimension=embedding_provider.dimension,
            max_size=kb_config.cache_max_size,
            default_ttl=kb_config.cache_ttl_seconds,
            similarity_threshold=kb_config.cache_similarity_threshold,
        )

        metrics = RagMetrics()

        # Build LLM client for Slow Thinker predictions
        llm_client = cls._build_llm_client(
            pred_api_key, pred_endpoint, azure_api_version
        )

        slow_thinker = SlowThinker(
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            cache=cache,
            metrics=metrics,
            max_predictions=kb_config.max_predictions,
            prefetch_top_k=kb_config.prefetch_top_k,
            rate_limit_secs=kb_config.slow_thinker_rate_limit,
            llm_client=llm_client,
            llm_model=pred_deployment,
        )

        fast_talker = FastTalker(
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            cache=cache,
            metrics=metrics,
            top_k=kb_config.top_k,
        )

        logger.info(
            "RagMemoryRouter built for %s/%s (pgvector store)",
            merchant_id,
            template_id,
        )

        return cls(
            fast_talker=fast_talker,
            slow_thinker=slow_thinker,
            cache=cache,
            metrics=metrics,
            kb_config=kb_config,
            merchant_id=merchant_id,
            template_id=template_id,
        )

    @staticmethod
    def _build_llm_client(
        api_key: Optional[str], endpoint: Optional[str], api_version: str
    ) -> Optional[object]:
        """Build an Azure OpenAI client for Slow Thinker predictions."""
        if not api_key or not endpoint:
            logger.warning(
                "SlowThinker LLM credentials missing — will use keyword fallback"
            )
            return None
        try:
            from openai import AzureOpenAI  # type: ignore

            return AzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
            )
        except ImportError:
            logger.warning(
                "openai package not available — Slow Thinker will use keyword fallback"
            )
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background Slow Thinker task."""
        await self._slow_thinker.start()

    async def stop(self) -> None:
        """Stop the background Slow Thinker task and log final metrics."""
        await self._slow_thinker.stop()
        m = self._metrics
        logger.info(
            "RagMemoryRouter stopped | queries=%d cache_hits=%d cache_misses=%d "
            "hit_rate=%.0f%% prefetch_ops=%d predictions=%d",
            m.total_queries,
            m.cache_hits,
            m.cache_misses,
            m.cache_hit_rate * 100,
            m.prefetch_operations,
            m.predictions_made,
        )

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    async def get_context(
        self,
        user_utterance: str,
        conversation_history: Optional[str] = None,
    ) -> str:
        """Return formatted context for the user's utterance.

        This should be called *synchronously in the LLM context-assembly phase*
        (i.e. after the STT transcript is available but before the first LLM
        token is generated).

        The SlowThinker is triggered in the background so it can pre-warm the
        cache for the *next* user turn while the LLM is currently responding.

        Args:
            user_utterance: The finalised STT transcript for the current turn.
            conversation_history: Optional formatted conversation history string
                (User: …\\nAssistant: …).  Used by SlowThinker for better
                prediction.  Pass ``None`` if not available.

        Returns:
            Formatted context string (empty string if nothing relevant found).
        """
        # Update local conversation history
        if user_utterance and user_utterance.strip():
            self._conversation_history.append(f"User: {user_utterance}")
            # Keep a rolling window to stay within the prompt budget
            if len(self._conversation_history) > 20:
                self._conversation_history = self._conversation_history[-20:]

        if not user_utterance or not user_utterance.strip():
            return ""

        history = conversation_history or "\n".join(self._conversation_history)

        # Trigger SlowThinker in the background (non-blocking)
        self._slow_thinker.on_user_utterance(user_utterance, history)

        # Retrieve context from cache / FAISS (fast path)
        context = await self._fast_talker.get_context(user_utterance)
        return context

    def record_assistant_response(self, response_text: str) -> None:
        """Call this after the bot finishes speaking to keep history up-to-date.

        Args:
            response_text: The full assistant response for the completed turn.
        """
        if response_text:
            self._conversation_history.append(f"Assistant: {response_text}")

    @property
    def metrics(self) -> RagMetrics:
        """Access live metrics for this router instance."""
        return self._metrics

    @property
    def knowledge_base_size(self) -> int:
        """Number of entries currently cached in the per-call semantic cache.

        The pgvector store is stateless (no in-process index), so the total
        number of indexed chunks is not available without a database round-trip.
        Use ``index_manager.get_cached_index_stats()`` for the persistent count.
        """
        return self._cache.size

    @property
    def gcs_path(self) -> str:
        """Full GCS path for this router's knowledge base."""
        gcs_bucket = RAG_GCS_BUCKET or ""
        return f"gs://{gcs_bucket}/{self._merchant_id}/{self._template_id}/"
