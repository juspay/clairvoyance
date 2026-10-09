"""The provider contract — one vendor behind ``generate``.

A provider owns everything about its vendor: the pooled client, the wire
format, retries and the error it raises. Callers see only this protocol and
the shapes in ``models``.
"""

from typing import Protocol

from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    ProviderConfig,
)


class ModelProvider(Protocol):
    # the provider's id, e.g. "openrouter"
    name: str

    @property
    def config(self) -> ProviderConfig:
        """How it reaches its vendor: URL, key, timeout, retries."""
        ...

    def with_config(self, config: ProviderConfig) -> "ModelProvider":
        """A new provider of this kind that reaches its vendor by ``config``."""
        ...

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        """One completion. Raises ``ProviderError`` on failure — never a
        partial response."""
        ...

    async def close(self) -> None:
        """Close the pooled client; safe when never opened."""
        ...
