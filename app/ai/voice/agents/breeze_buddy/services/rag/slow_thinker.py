"""
SlowThinker – background async agent for Breeze Buddy RAG.

Subscribes to the conversation stream, predicts likely follow-up topics using
the Azure OpenAI LLM, retrieves relevant chunks from the pgvector store, and
pre-warms the semantic cache.  All of this happens in the background while the
bot is generating and speaking its response, so the *next* user turn finds
context already waiting in the cache.

Key design points:
- Prediction and prefetch tasks run in parallel via ``asyncio.gather``.
- All embed calls for a single turn are batched into one HTTP request.
- A rate limiter prevents prediction storms during rapid turns.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from app.ai.voice.agents.breeze_buddy.services.rag.embeddings import EmbeddingProvider
from app.ai.voice.agents.breeze_buddy.services.rag.semantic_cache import SemanticCache
from app.ai.voice.agents.breeze_buddy.services.rag.types import RagMetrics
from app.ai.voice.agents.breeze_buddy.services.rag.vector_store import PgVectorStore
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PREDICT_PROMPT = """\
Conversation so far:
{conversation}

Latest user message: {latest}

List {n} short document-search phrases (NOT questions) predicting what the user
will ask about next. Each phrase should match knowledge-base content.
Reply with a numbered list only — nothing else.
1."""


class SlowThinker:
    """Background async agent that pre-warms the semantic cache.

    Args:
        vector_store: The knowledge-base vector store (``PgVectorStore``).
        embedding_provider: Shared embedding provider.
        cache: The semantic cache to populate.
        metrics: Shared metrics instance.
        max_predictions: How many follow-up topics to predict per turn.
        prefetch_top_k: Chunks to retrieve per predicted topic.
        rate_limit_secs: Minimum seconds between prediction cycles.
        llm_client: An ``openai.AzureOpenAI`` client for predictions.
            If ``None``, keyword-based fallback is used.
        llm_model: Azure deployment name for the prediction LLM.
    """

    def __init__(
        self,
        vector_store: PgVectorStore,
        embedding_provider: EmbeddingProvider,
        cache: SemanticCache,
        metrics: RagMetrics,
        max_predictions: int = 4,
        prefetch_top_k: int = 10,
        rate_limit_secs: float = 0.5,
        llm_client: Optional[object] = None,
        llm_model: str = "gpt-4o-mini",
    ) -> None:
        self._store = vector_store
        self._embeddings = embedding_provider
        self._cache = cache
        self._metrics = metrics
        self._max_predictions = max_predictions
        self._prefetch_top_k = prefetch_top_k
        self._rate_limit = rate_limit_secs
        self._llm = llm_client
        self._llm_model = llm_model

        self._running = False
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._queue: asyncio.Queue[tuple] = asyncio.Queue(maxsize=20)  # type: ignore[type-arg]
        self._last_run: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background processing loop."""
        self._running = True
        self._task = asyncio.create_task(self._run(), name="rag-slow-thinker")
        logger.debug("SlowThinker started")

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.debug("SlowThinker stopped")

    # ------------------------------------------------------------------
    # Public API (called from MemoryRouter on each user utterance)
    # ------------------------------------------------------------------

    def on_user_utterance(self, text: str, conversation_history: str) -> None:
        """Enqueue a user utterance for background processing.

        Non-blocking — drops silently if the queue is full (back-pressure).
        """
        try:
            self._queue.put_nowait(("utterance", text, conversation_history))
        except asyncio.QueueFull:
            logger.debug("SlowThinker queue full — dropping utterance")

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while self._running:
            try:
                kind, text, history = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
                now = time.time()
                remaining = self._rate_limit - (now - self._last_run)
                if remaining > 0:
                    # Rate-limited: wait out the remainder, then process the
                    # latest utterance (which may have already replaced stale
                    # ones via the queue drain below).
                    await asyncio.sleep(remaining)
                    # Drain any queued-up items; keep only the most recent one.
                    latest_kind, latest_text, latest_history = kind, text, history
                    while not self._queue.empty():
                        try:
                            latest_kind, latest_text, latest_history = (
                                self._queue.get_nowait()
                            )
                        except asyncio.QueueEmpty:
                            break
                    kind, text, history = latest_kind, latest_text, latest_history

                self._last_run = time.time()

                if kind == "utterance":
                    await self._handle_utterance(text, history)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("SlowThinker error: %s", exc, exc_info=True)

    async def _handle_utterance(self, text: str, conversation_history: str) -> None:
        """Predict follow-up topics and prefetch context into the cache.

        All queries (current utterance + predictions) are embedded in a single
        batched HTTP call to reduce Azure round-trips from N+1 to 1.
        """
        predictions = await self._predict_followups(text, conversation_history)
        if predictions:
            self._metrics.predictions_made += len(predictions)

        # Batch all embed calls: [current_utterance] + predictions
        all_queries = [text] + predictions
        try:
            all_embeddings = await self._embeddings.embed(all_queries)
        except Exception as exc:
            logger.warning("SlowThinker batch embed failed: %s", exc)
            return

        # Launch all retrieval+cache tasks in parallel
        tasks = [
            asyncio.create_task(
                self._retrieve_and_cache(all_queries[i], all_embeddings[i])
            )
            for i in range(len(all_queries))
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Core retrieval
    # ------------------------------------------------------------------

    async def _retrieve_and_cache(
        self, query: str, query_embedding: "np.ndarray"  # type: ignore[name-defined]
    ) -> None:
        """Search the vector store with a pre-computed embedding and populate the cache."""
        try:
            results = await self._store.search(
                query_embedding, top_k=self._prefetch_top_k
            )
            await self._cache.put_batch(
                [
                    {
                        "query_embedding": query_embedding,
                        "text": r.text,
                        "metadata": r.metadata,
                        "relevance_score": r.score,
                    }
                    for r in results
                ]
            )
            self._metrics.prefetch_operations += 1
            logger.debug(
                "SlowThinker: prefetched %d chunks for '%s…'",
                len(results),
                query[:40],
            )
        except Exception as exc:
            logger.warning("SlowThinker retrieval error: %s", exc)

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    async def _predict_followups(
        self, latest: str, conversation_history: str
    ) -> List[str]:
        """Predict likely follow-up topics using the LLM (or keyword fallback)."""
        if self._llm is None:
            return self._keyword_fallback(latest)
        try:
            return await self._predict_with_llm(latest, conversation_history)
        except Exception as exc:
            logger.warning(
                "SlowThinker LLM prediction failed (%s); using keywords", exc
            )
            return self._keyword_fallback(latest)

    async def _predict_with_llm(
        self, latest: str, conversation_history: str
    ) -> List[str]:
        """Call Azure OpenAI for follow-up topic predictions."""
        prompt = _PREDICT_PROMPT.format(
            conversation=(
                conversation_history[-1500:] if conversation_history else "(none)"
            ),
            latest=latest,
            n=self._max_predictions,
        )

        response = await asyncio.to_thread(self._llm_call_sync, prompt)  # type: ignore[arg-type]

        predictions: List[str] = []
        for line in response.strip().splitlines():
            line = line.strip()
            if line and line[0].isdigit():
                line = line.split(".", 1)[-1].strip()
            elif line.startswith("- "):
                line = line[2:].strip()
            if line:
                predictions.append(line)
        return predictions[: self._max_predictions]

    def _llm_call_sync(self, prompt: str) -> str:
        """Synchronous Azure OpenAI chat completion (runs in thread pool)."""
        response = self._llm.chat.completions.create(  # type: ignore[union-attr]
            model=self._llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.3,
        )
        return response.choices[0].message.content or ""

    def _keyword_fallback(self, text: str) -> List[str]:
        """Simple keyword extraction as fallback when LLM is unavailable."""
        words = [w.strip(".,!?;:") for w in text.split() if len(w) > 4]
        if words:
            return [" ".join(words[:6])]
        return []
