"""The adapter interface, and the generic adapter every platform extends.

Rule (ASSIST-ENGINE-DESIGN.md §1): the engine works on any public website
with ZERO adapters — ``GenericAdapter`` is that behaviour. A platform adds
by overriding hooks, never by forking a stage.
"""

from __future__ import annotations

from typing import List, Protocol, Sequence
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    InstallMethod,
    MirrorPolicy,
    ResearchDelta,
    Signal,
    SiteProfile,
    StoreResearch,
    TenantIdentity,
    ToolBinding,
)


class PlatformAdapter(Protocol):
    id: str

    def classify(self, signals: Sequence[Signal]) -> float: ...

    def identity(self, profile: SiteProfile) -> TenantIdentity: ...

    async def research(
        self, profile: SiteProfile, budget_seconds: float
    ) -> ResearchDelta: ...

    def operating_sections(self) -> List[str]: ...

    def tools(
        self, identity: TenantIdentity, research: StoreResearch
    ) -> List[ToolBinding]: ...

    def extra_origins(
        self, identity: TenantIdentity, research: StoreResearch
    ) -> List[str]: ...

    def mirror_policy(self) -> MirrorPolicy: ...

    def install(self) -> InstallMethod: ...


_GENERIC_BLOCKED = [
    "/checkout",
    "/cart/checkout",
    "/account",
    "/login",
    "/signin",
    "/register",
    "/my-account",
    "/customer",
    "/wp-login.php",
    "/wp-admin",
    "/admin",
    "/payment",
]


class GenericAdapter:
    """Any public website: no platform facts, catalogue from research only."""

    id = "generic"

    def classify(self, signals: Sequence[Signal]) -> float:
        return 0.0

    def identity(self, profile: SiteProfile) -> TenantIdentity:
        host = (urlsplit(profile.final_url or profile.url).hostname or "").lower()
        return TenantIdentity(platform=self.id, canonical_host=host)

    async def research(
        self, profile: SiteProfile, budget_seconds: float
    ) -> ResearchDelta:
        return ResearchDelta()

    def operating_sections(self) -> List[str]:
        return []

    def tools(
        self, identity: TenantIdentity, research: StoreResearch
    ) -> List[ToolBinding]:
        return []

    def extra_origins(
        self, identity: TenantIdentity, research: StoreResearch
    ) -> List[str]:
        return list(research.extra_origins)

    def mirror_policy(self) -> MirrorPolicy:
        return MirrorPolicy(blocked_paths=list(_GENERIC_BLOCKED), cart_handoff="link")

    def install(self) -> InstallMethod:
        return "snippet"


__all__ = ["GenericAdapter", "PlatformAdapter"]
