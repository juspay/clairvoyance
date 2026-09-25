"""CONVERSATION_EVALS providers — the vendors an engine can be served by.

A provider wraps exactly one vendor client behind the
``ConversationEvalsProvider`` contract and owns that client's pool.
``PROVIDERS`` holds the one instance of each; engines pick theirs from it
by name and route on the config row's ``provider``. Nothing above a
provider imports a vendor module directly.
"""

from typing import Dict

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers.base import (
    ConversationEvalsProvider,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers.typesafe import (
    TypeSafeError,
    TypeSafeProvider,
)

PROVIDERS: Dict[str, ConversationEvalsProvider] = {"typesafe": TypeSafeProvider()}


async def close_conversation_evals_provider_pools() -> None:
    """Shutdown hook: drain every provider's pooled client, once each.

    Mirrors ``close_llm_http_pools`` — one call from the lifespan; a new
    vendor is one provider class + one entry in ``PROVIDERS``.
    """
    for provider in PROVIDERS.values():
        await provider.close()


__all__ = [
    "PROVIDERS",
    "ConversationEvalsProvider",
    "TypeSafeError",
    "TypeSafeProvider",
    "close_conversation_evals_provider_pools",
]
