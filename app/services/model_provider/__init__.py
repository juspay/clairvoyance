"""Model provider registry.

Providers are keyed by ``name``. Adding a vendor is one module implementing
``ModelProvider`` + one registry entry here; callers only use
``get_provider`` and the shapes in ``models``.
"""

from typing import Mapping

from app.core.config.static import (
    OPENROUTER_API_KEY,
    OPENROUTER_API_URL,
    OPENROUTER_TIMEOUT_SECONDS,
    TYPESAFE_API_KEY,
    TYPESAFE_API_URL,
    TYPESAFE_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.services.model_provider.base import ModelProvider
from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    GenerationSettings,
    Input,
    JsonSchema,
    Message,
    ProviderConfig,
    ProviderError,
    Usage,
)
from app.services.model_provider.openrouter import OpenRouterProvider
from app.services.model_provider.typesafe import TypeSafeProvider

OPENROUTER = OpenRouterProvider(
    ProviderConfig(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_API_URL,
        timeout_seconds=OPENROUTER_TIMEOUT_SECONDS,
    )
)
TYPESAFE = TypeSafeProvider(
    ProviderConfig(
        api_key=TYPESAFE_API_KEY,
        base_url=TYPESAFE_API_URL,
        timeout_seconds=TYPESAFE_TIMEOUT_SECONDS,
    )
)

PROVIDERS: Mapping[str, ModelProvider] = {
    OPENROUTER.name: OPENROUTER,
    TYPESAFE.name: TYPESAFE,
}


def get_provider(name: str) -> ModelProvider:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(name, "unknown provider")
    return provider


async def close_all() -> None:
    """Close every provider's pooled client (process shutdown). One failing
    close never stops the others, nor the rest of the shutdown."""
    for provider in PROVIDERS.values():
        try:
            await provider.close()
        except Exception as exc:
            logger.warning(f"closing model provider {provider.name!r} failed: {exc}")


__all__ = [
    "GenerateRequest",
    "GenerateResponse",
    "GenerationSettings",
    "Input",
    "JsonSchema",
    "Message",
    "ModelProvider",
    "OPENROUTER",
    "OpenRouterProvider",
    "PROVIDERS",
    "ProviderConfig",
    "ProviderError",
    "TYPESAFE",
    "TypeSafeProvider",
    "Usage",
    "close_all",
    "get_provider",
]
