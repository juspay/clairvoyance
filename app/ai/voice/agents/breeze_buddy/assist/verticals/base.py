"""The vertical interface — the second axis the engine must not know."""

from __future__ import annotations

from typing import Dict, Mapping, Protocol

from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import SkeletonSpec


class Vertical(Protocol):
    id: str
    # The value the onboarding API uses for this vertical (``vertical`` field).
    request_vertical: str
    # The reseller-level blueprint template this vertical's agents are built from.
    blueprint_name: str
    skeleton: SkeletonSpec

    def research_prompt(self) -> str:
        """The brief handed to the site reader (facts only, never instructions)."""
        ...

    def brand_block(
        self, assistant_name: str, brand_name: str, website_context: str
    ) -> str:
        """What replaces the skeleton's brand marker for one merchant."""
        ...

    def unpersonalized_context(self) -> str:
        """The website context used when no site read has happened yet."""
        ...

    def payload_schema(self, site_host: str) -> Mapping[str, Dict[str, object]]:
        """Payload-schema entries every agent of this vertical carries."""
        ...

    def secrets(self, site_host: str) -> Mapping[str, str]:
        """Server-owned secret defaults every agent of this vertical carries."""
        ...


__all__ = ["Vertical"]
