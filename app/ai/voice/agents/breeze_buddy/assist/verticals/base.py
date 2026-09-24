"""The vertical interface — the second axis the engine must not know."""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Protocol, Sequence

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

    def brand_block_from_fields(
        self,
        fields: Mapping[str, Sequence[str]],
        *,
        assistant_name: str,
        brand_name: str,
        site_host: str,
    ) -> str:
        """The same block, built from the fields a merchant can edit.

        The fields are this vertical's own — the ones its studio shows — so
        the block onboarding writes and the block the studio writes are the
        same block, assembled by the same rules. A vertical with no studio
        can ignore this and keep the string path.
        """
        ...

    def vertical_section(self, fields: Mapping[str, Sequence[str]]) -> Optional[str]:
        """The merchant-specific help section, when the fields supply one."""
        ...

    def widget_values(self, fields: Mapping[str, Sequence[str]]) -> Dict[str, object]:
        """Configuration entries these fields imply: greeting, replies, links."""
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
