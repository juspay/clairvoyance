"""The provider contract — one vendor behind ``generate``.

A provider owns everything about its vendor: the pooled client, the wire
format, retries and the error it raises. Callers see only this protocol and
the shapes in ``models``.
"""

from typing import Protocol

from app.services.model_provider.models import GenerateRequest, GenerateResponse


class ModelProvider(Protocol):
    # the provider's id, e.g. "openrouter"
    name: str

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        """One completion. Raises ``ProviderError`` on failure — never a
        partial response."""
        ...

    async def close(self) -> None:
        """Close the pooled client; safe when never opened."""
        ...
