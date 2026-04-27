"""
Breeze Buddy RAG Service

Voice-optimised Retrieval-Augmented Generation using a dual-agent architecture:

  SlowThinker  – background async task that predicts follow-up topics from the
                 ongoing conversation, retrieves candidate chunks from the
                 per-template pgvector store and pre-warms the SemanticCache.

  FastTalker   – synchronous (sub-ms) cache lookup.  Falls back to direct
                 pgvector search only on a cache miss, so the LLM context is
                 always ready before the first TTS byte is produced.

Knowledge files live in GCS under a configurable prefix. They are downloaded
once per index run, chunked, embedded (Azure OpenAI text-embedding-3-small) and
stored in PostgreSQL via pgvector.  The IndexManager writes embeddings into the
``rag_embeddings`` table; the PgVectorStore fetches them on demand per call.

Usage
-----
from app.ai.voice.agents.breeze_buddy.services.rag import RagMemoryRouter

router = await RagMemoryRouter.build(
    kb_config=template.configurations.knowledge_base,
    merchant_id=merchant_id,
    template_id=template_id,
)
await router.start()

# In the LLM context-building phase:
context = await router.get_context(user_utterance)

await router.stop()
"""

from app.ai.voice.agents.breeze_buddy.services.rag.memory_router import RagMemoryRouter
from app.ai.voice.agents.breeze_buddy.services.rag.types import (
    KnowledgeBaseConfig,
    RagMetrics,
)

__all__ = [
    "RagMemoryRouter",
    "KnowledgeBaseConfig",
    "RagMetrics",
]
