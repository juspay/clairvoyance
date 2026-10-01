"""Eval providers — the vendors an engine can be served by.

A provider wraps exactly one vendor behind the ``EvalProvider`` contract
(``ProviderRequest`` in, ``ProviderResponse`` out) and owns that vendor's
client. ``PROVIDERS`` holds the one instance of each; engines reference
those instances and route on the config row's ``provider``. Nothing above
a provider imports a vendor module directly.
"""

from typing import Dict

from app.ai.voice.agents.breeze_buddy.services.evals.providers.base import (
    EvalProvider,
    ProviderRequest,
    ProviderResponse,
)
from app.ai.voice.agents.breeze_buddy.services.evals.providers.typesafe import (
    TypeSafeError,
    TypeSafeProvider,
)

TYPESAFE: EvalProvider = TypeSafeProvider()

PROVIDERS: Dict[str, EvalProvider] = {TYPESAFE.name: TYPESAFE}


async def close_eval_provider_pools() -> None:
    """Shutdown hook: drain every provider's pooled client, once each.

    Mirrors ``close_llm_http_pools`` — one call from the lifespan; a new
    vendor is one provider class + one entry in ``PROVIDERS``.
    """
    for provider in PROVIDERS.values():
        await provider.close()


__all__ = [
    "PROVIDERS",
    "TYPESAFE",
    "EvalProvider",
    "ProviderRequest",
    "ProviderResponse",
    "TypeSafeError",
    "TypeSafeProvider",
    "close_eval_provider_pools",
]
