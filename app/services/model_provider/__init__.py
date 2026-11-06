"""Model provider registry.

Providers are keyed by ``name``. Adding a vendor is one module implementing
``ModelProvider`` + one registry entry here; callers only use
``get_provider`` and the shapes in ``models``. ``resolve_provider`` points a
registered provider at another URL and key, named by config keys.
"""

from dataclasses import replace
from typing import Any, Dict, Mapping, Tuple

from app.core.config.static import (
    OPENROUTER_API_KEY,
    OPENROUTER_API_URL,
    OPENROUTER_TIMEOUT_SECONDS,
    TYPESAFE_API_KEY,
    TYPESAFE_API_URL,
    TYPESAFE_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.services.live_config.store import get_config
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


# providers pointed elsewhere by resolve_provider, one per (provider, URL, key),
# so each keeps one pooled client; close_all closes them too
_NAMED: Dict[Tuple[str, str, str], ModelProvider] = {}


async def resolve_provider(configuration: Mapping[str, Any]) -> ModelProvider:
    """The provider an eval's ``configuration`` names (``provider``), as
    registered, or the same provider at the URL and with the key that the
    config keys it names in ``api_url_name`` / ``api_key_name`` hold (Redis,
    then the environment, as a template's llm_configuration
    ``api_key_name``). A URL without a key is refused: it would send this
    provider's own key to another host. Raises ``ProviderError``."""
    provider = get_provider(configuration["provider"])
    api_url_name = configuration.get("api_url_name")
    api_key_name = configuration.get("api_key_name")
    if not api_url_name and not api_key_name:
        return provider
    if not api_key_name:
        raise ProviderError(
            provider.name, "api_key_name is required when api_url_name is given"
        )
    api_key = await get_config(api_key_name, "", str)
    if not api_key:
        raise ProviderError(
            provider.name, f"API key not found for config key: {api_key_name}"
        )
    base_url = provider.config.base_url
    if api_url_name:
        base_url = await get_config(api_url_name, "", str)
        if not base_url:
            raise ProviderError(
                provider.name, f"API URL not found for config key: {api_url_name}"
            )
    key = (provider.name, base_url, api_key)
    named = _NAMED.get(key)
    if named is None:
        named = provider.with_config(
            replace(provider.config, base_url=base_url, api_key=api_key)
        )
        _NAMED[key] = named
    return named


async def close_all() -> None:
    """Close every provider's pooled client (process shutdown). One failing
    close never stops the others, nor the rest of the shutdown."""
    for provider in [*PROVIDERS.values(), *_NAMED.values()]:
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
    "resolve_provider",
]
