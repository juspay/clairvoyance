"""
OpenAI (direct) embeddings provider — explicit opt-in; the platform default
is the in-region ``azure_openai`` provider.

Calls the REST endpoint directly through the shared aiohttp factory (proxy
aware) -- no SDK dependency. ``text-embedding-3-small`` supports the
``dimensions`` parameter natively, so truncation happens server-side and the
L2 renorm here is a cheap safety pass.
"""

from typing import List, Optional

import aiohttp

from app.core.config.static import OPENAI_API_KEY
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.services.embeddings.base import (
    TARGET_DIMENSIONS,
    EmbeddingProvider,
    InputType,
    truncate_and_normalize,
)

_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# One lazily-created session shared across calls (connection reuse matters on
# the query hot path). aiohttp sessions are cheap to hold and die with the
# process.
_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    """Lazily create/reuse the module's shared aiohttp session.

    Re-created if closed. Keeping one session gives TLS/connection reuse,
    which matters for the per-turn query embedding on the voice hot path.
    """
    global _session
    if _session is None or _session.closed:
        _session = create_aiohttp_session(
            timeout=aiohttp.ClientTimeout(total=30, connect=5)
        )
    return _session


class OpenAIEmbeddingProvider(EmbeddingProvider):
    provider_key = "openai"

    def __init__(self, model: str = "text-embedding-3-small"):
        """Fail fast at construction (not first call) when the API key is
        missing — surfaces misconfiguration at ingestion kick / first
        retrieval instead of as a confusing mid-call HTTP 401.

        Constructed only via the registry factory in
        ``app/services/embeddings/__init__.py`` (one instance per model).
        """
        if not OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY is not configured; cannot use the openai "
                "embedding provider"
            )
        self.model = model

    async def embed(
        self, texts: List[str], input_type: InputType = "document"
    ) -> List[List[float]]:
        """Embed a batch of texts into 768-dim unit vectors.

        Called from ingestion (document chunks, batched) and retrieval
        (single query string, behind the Redis cache). ``input_type`` is
        ignored — OpenAI doesn't distinguish query vs document embeddings.
        Raises RuntimeError on non-200; callers own retry/fail-open policy.
        """
        if not texts:
            return []

        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": TARGET_DIMENSIONS,
        }
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

        async with _get_session().post(
            _OPENAI_EMBEDDINGS_URL, json=payload, headers=headers
        ) as response:
            if response.status != 200:
                body = await response.text()
                logger.error(
                    f"OpenAI embeddings request failed ({response.status}): {body[:500]}"
                )
                raise RuntimeError(
                    f"OpenAI embeddings request failed with status {response.status}"
                )
            data = await response.json()

        # The API returns items with an ``index`` field; order defensively.
        items = sorted(data["data"], key=lambda item: item["index"])
        return [truncate_and_normalize(item["embedding"]) for item in items]
