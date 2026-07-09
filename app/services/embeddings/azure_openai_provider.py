"""
Azure OpenAI embeddings provider — the platform default (EmbeddingConfig
defaults here; other providers are explicit opt-in per KB).

Same wire format as the OpenAI provider but served from an in-region Azure
deployment, which is what keeps query-embedding traffic inside the voice
budget: retrieval must finish within ``retrieval_timeout_secs`` (0.4s
default), and a Standard deployment in Azure South India answers in
~70-90ms warm vs 400ms+ cross-region.

Credentials come from the ``KB_AZURE_OPENAI_ENDPOINT`` /
``KB_AZURE_OPENAI_API_KEY`` dynamic-config keys (Redis override → env),
resolved on every call so the key can be rotated at runtime without a
deploy. They are deliberately NOT the LLM's static ``AZURE_OPENAI_*``
vars — embeddings and LLM must be repointable/rotatable independently.

``model`` on the KB's ``embedding_config`` is the Azure DEPLOYMENT name,
not an OpenAI model id (they coincide when the deployment is named after
its model, e.g. ``text-embedding-3-large``). Third-generation embedding
deployments support the ``dimensions`` parameter, so truncation to 768
happens server-side like the OpenAI provider.
"""

from typing import List, Optional

import aiohttp

from app.core.config.dynamic import (
    KB_AZURE_OPENAI_API_KEY,
    KB_AZURE_OPENAI_ENDPOINT,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.services.embeddings.base import (
    TARGET_DIMENSIONS,
    EmbeddingProvider,
    InputType,
    truncate_and_normalize,
)

_API_VERSION = "2024-10-21"

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


class AzureOpenAIEmbeddingProvider(EmbeddingProvider):
    provider_key = "azure_openai"

    def __init__(self, model: str = "text-embedding-3-large"):
        """``model`` is the Azure deployment name. Endpoint/key are dynamic
        config, so (unlike the OpenAI provider) missing credentials surface
        on the first ``embed`` call, not at construction.

        Constructed only via the registry factory in
        ``app/services/embeddings/__init__.py`` (one instance per model).
        """
        self.model = model

    async def embed(
        self, texts: List[str], input_type: InputType = "document"
    ) -> List[List[float]]:
        """Embed a batch of texts into 768-dim unit vectors.

        Called from ingestion (document chunks, batched) and retrieval
        (single query string). ``input_type`` is ignored — OpenAI models
        don't distinguish query vs document embeddings. Raises ValueError
        when the KB_AZURE_OPENAI_* config is missing and RuntimeError on
        non-200; callers own retry/fail-open policy.
        """
        if not texts:
            return []

        endpoint = await KB_AZURE_OPENAI_ENDPOINT()
        api_key = await KB_AZURE_OPENAI_API_KEY()
        if not endpoint or not api_key:
            raise ValueError(
                "KB_AZURE_OPENAI_ENDPOINT / KB_AZURE_OPENAI_API_KEY are not "
                "configured; cannot use the azure_openai embedding provider"
            )

        url = (
            f"{endpoint.rstrip('/')}/openai/deployments/"
            f"{self.model}/embeddings?api-version={_API_VERSION}"
        )
        payload = {"input": texts, "dimensions": TARGET_DIMENSIONS}
        headers = {"api-key": api_key}

        async with _get_session().post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                body = await response.text()
                logger.error(
                    f"Azure OpenAI embeddings request failed "
                    f"({response.status}): {body[:500]}"
                )
                raise RuntimeError(
                    f"Azure OpenAI embeddings request failed with status "
                    f"{response.status}"
                )
            data = await response.json()

        # The API returns items with an ``index`` field; order defensively.
        items = sorted(data["data"], key=lambda item: item["index"])
        return [truncate_and_normalize(item["embedding"]) for item in items]
