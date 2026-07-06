"""
Embedding provider abstraction.

Every provider returns vectors normalized to a fixed dimension count
(``TARGET_DIMENSIONS``) via Matryoshka truncation + L2 renormalization, so a
single ``halfvec(768)`` column/index serves every knowledge base regardless
of provider. Adding a provider = one module implementing ``EmbeddingProvider``
+ one registry entry in ``app/services/embeddings/__init__.py``.
"""

import math
from abc import ABC, abstractmethod
from typing import List, Literal

TARGET_DIMENSIONS = 768

InputType = Literal["query", "document"]


class EmbeddingProvider(ABC):
    """Async embedding provider interface."""

    provider_key: str = ""

    @abstractmethod
    async def embed(
        self, texts: List[str], input_type: InputType = "document"
    ) -> List[List[float]]:
        """Embed a batch of texts into TARGET_DIMENSIONS-dim unit vectors.

        ``input_type`` lets providers that distinguish query vs document
        embeddings (Cohere, Voyage) use the right mode; providers that don't
        (OpenAI) ignore it.
        """
        raise NotImplementedError


def truncate_and_normalize(
    vector: List[float], dims: int = TARGET_DIMENSIONS
) -> List[float]:
    """Matryoshka truncation to ``dims`` + L2 renormalization.

    The normalization step every provider funnels through so all stored
    vectors are 768-dim unit vectors — one halfvec column/index serves
    every provider, and cosine distance stays valid after truncation.
    Called by each provider's ``embed`` on every vector it returns.
    """
    if len(vector) < dims:
        # Fail here with a clear message instead of at the halfvec(768)
        # insert boundary with an opaque dimension-mismatch error.
        raise ValueError(
            f"Embedding has {len(vector)} dimensions, expected at least {dims}"
        )
    truncated = vector[:dims]
    norm = math.sqrt(sum(v * v for v in truncated))
    if norm == 0:
        return truncated
    return [v / norm for v in truncated]
