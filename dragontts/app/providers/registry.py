"""Provider registry — builds adapters for every provider with a configured key.

Providers are imported lazily inside ``build()`` so a missing optional key
(e.g. no Gemini credentials) never breaks startup. ElevenLabs runs only against
the Indian-residency endpoint (the only account with API access); the single
``elevenlabs`` route resolves to it.
"""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import logger
from app.providers.base import BaseTTSProvider


class ProviderNotConfigured(Exception):
    """Raised when a request routes to a provider whose API key is absent."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(
            f"Provider '{provider}' is not configured. Set its API key in the "
            f"environment and DragonTTS will serve it after restart."
        )


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, BaseTTSProvider] = {}

    def build(self) -> None:
        if settings.cartesia_api_key:
            from app.providers.cartesia import CartesiaProvider

            self._providers["cartesia"] = CartesiaProvider()

        if settings.sarvam_api_key:
            from app.providers.sarvam import SarvamProvider

            self._providers["sarvam"] = SarvamProvider()

        if settings.elevenlabs_indian_residency_api_key:
            from app.providers.elevenlabs import ElevenLabsProvider

            # Single ElevenLabs route -> Indian-residency creds (constructor
            # defaults). No global route: only the residency account has access.
            self._providers["elevenlabs"] = ElevenLabsProvider()

        if settings.google_credentials_json or settings.google_credentials_path:
            from app.providers.gemini import GeminiProvider

            self._providers["gemini"] = GeminiProvider(
                credentials_json=settings.google_credentials_json or None,
                credentials_path=settings.google_credentials_path or None,
            )

        logger.info(f"Configured providers: {self.configured()}")

    def get(self, name: str) -> BaseTTSProvider | None:
        return self._providers.get(name)

    def configured(self) -> list[str]:
        return list(self._providers)

    async def warm(self) -> None:
        """Pre-warm provider resources (e.g. the Cartesia streaming socket pool).

        Warmed concurrently (the providers are independent) so startup latency is
        the max, not the sum. Failures are logged, never fatal — a provider still
        works via lazy warm-up if its eager warm-up failed.
        """

        async def _warm(provider: BaseTTSProvider) -> None:
            try:
                await provider.warm()
            except Exception as e:
                logger.warning(f"Provider '{provider.name}' warm-up failed: {e}")

        await asyncio.gather(*(_warm(p) for p in self._providers.values()))

    async def aclose_all(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
