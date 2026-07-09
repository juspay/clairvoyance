"""
Embedding provider registry.

Providers are keyed by ``EmbeddingConfig.provider`` (stored per knowledge
base). Adding a provider (Gemini, Cohere, ...) is one module + one registry
entry -- retrieval and ingestion are provider-agnostic.
"""

from typing import Callable, Dict

from app.schemas.breeze_buddy.knowledge_base import EmbeddingConfig
from app.services.embeddings.azure_openai_provider import (
    AzureOpenAIEmbeddingProvider,
)
from app.services.embeddings.base import (
    TARGET_DIMENSIONS,
    EmbeddingProvider,
    truncate_and_normalize,
)
from app.services.embeddings.openai_provider import OpenAIEmbeddingProvider

_PROVIDER_REGISTRY: Dict[str, Callable[[str], EmbeddingProvider]] = {
    "openai": lambda model: OpenAIEmbeddingProvider(model=model),
    # DEFAULT (EmbeddingConfig defaults here): in-region Azure deployment of
    # the same models; ``model`` is the Azure deployment name. See the
    # provider module for the latency rationale.
    "azure_openai": lambda model: AzureOpenAIEmbeddingProvider(model=model),
}

# Instances are stateless besides the shared HTTP session; cache per
# (provider, model) so hot-path calls skip construction.
_instances: Dict[str, EmbeddingProvider] = {}


def get_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Resolve (and cache) an EmbeddingProvider from a KB's embedding_config.

    The two call sites are the seams where providers plug in: ingestion's
    embed loop and retrieval's ``_embed_query``. Instances are cached
    per ``provider:model`` — they're stateless besides the shared HTTP
    session, so one per process is enough. Unknown providers raise
    immediately with the list of valid keys.
    """
    key = f"{config.provider}:{config.model}"
    if key in _instances:
        return _instances[key]

    factory = _PROVIDER_REGISTRY.get(config.provider)
    if factory is None:
        raise ValueError(
            f"Unknown embedding provider '{config.provider}'. "
            f"Available: {sorted(_PROVIDER_REGISTRY)}"
        )
    instance = factory(config.model)
    _instances[key] = instance
    return instance


__all__ = [
    "EmbeddingProvider",
    "TARGET_DIMENSIONS",
    "get_embedding_provider",
    "truncate_and_normalize",
]
