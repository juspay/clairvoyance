"""The adapter interface, and the generic adapter every platform extends.

Rule (ASSIST-ENGINE-DESIGN.md §1): the engine works on any public website
with ZERO adapters — ``GenericAdapter`` is that behaviour. A platform adds
by overriding hooks, never by forking a stage. Everything a build stage
used to branch on is a hook here: which prompt sections to keep, which MCP
servers and config entries belong to the platform, which payload keys it
needs, how a host app maps to a tenant, how a blueprint must look.
"""

from __future__ import annotations

from typing import FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    InstallMethod,
    MirrorPolicy,
    ResearchDelta,
    Signal,
    SiteProfile,
    SiteResearch,
    TenantIdentity,
    ToolBinding,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import LegacyMarkers


class PlatformAdapter(Protocol):
    id: str
    request_platform: str
    host_apps: Tuple[str, ...]
    # The vertical this platform serves, or None when any (a plain website).
    vertical: Optional[str]

    def classify(self, signals: Sequence[Signal]) -> float: ...

    def identity(self, profile: SiteProfile) -> TenantIdentity: ...

    def tenant(self, host_app: str, merchant_domain: str) -> Tuple[str, str]: ...

    def store_name(self, merchant_domain: str) -> str: ...

    async def research(
        self, profile: SiteProfile, budget_seconds: float
    ) -> ResearchDelta: ...

    def legacy_section_markers(self) -> LegacyMarkers: ...

    def validate_blueprint(
        self, prompt: str, configurations: Mapping[str, object]
    ) -> List[str]: ...

    def mcp_server_names(self) -> FrozenSet[str]: ...

    def tool_config_keys(self) -> Tuple[str, ...]: ...

    def payload_keys(self) -> Tuple[str, ...]: ...

    def tools(
        self, identity: TenantIdentity, research: SiteResearch
    ) -> List[ToolBinding]: ...

    def extra_origins(
        self, identity: TenantIdentity, research: SiteResearch
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
    # The value the onboarding API uses for this adapter (``platform`` field).
    request_platform = "web"
    vertical: Optional[str] = None
    # Host apps (install-time callers) that land on this adapter: none.
    host_apps: Tuple[str, ...] = ()

    def classify(self, signals: Sequence[Signal]) -> float:
        return 0.0

    def identity(self, profile: SiteProfile) -> TenantIdentity:
        host = (urlsplit(profile.final_url or profile.url).hostname or "").lower()
        return TenantIdentity(platform=self.id, canonical_host=host)

    def tenant(self, host_app: str, merchant_domain: str) -> Tuple[str, str]:
        raise ValueError(f"host app {host_app!r} has no tenant namespace on {self.id}")

    def store_name(self, merchant_domain: str) -> str:
        return merchant_domain

    async def research(
        self, profile: SiteProfile, budget_seconds: float
    ) -> ResearchDelta:
        return ResearchDelta()

    def legacy_section_markers(self) -> LegacyMarkers:
        return {}

    def validate_blueprint(
        self, prompt: str, configurations: Mapping[str, object]
    ) -> List[str]:
        return []

    def mcp_server_names(self) -> FrozenSet[str]:
        return frozenset()

    def tool_config_keys(self) -> Tuple[str, ...]:
        return ()

    def payload_keys(self) -> Tuple[str, ...]:
        return ()

    def tools(
        self, identity: TenantIdentity, research: SiteResearch
    ) -> List[ToolBinding]:
        return []

    def extra_origins(
        self, identity: TenantIdentity, research: SiteResearch
    ) -> List[str]:
        return list(research.extra_origins)

    def mirror_policy(self) -> MirrorPolicy:
        return MirrorPolicy(blocked_paths=list(_GENERIC_BLOCKED), handoff="link")

    def install(self) -> InstallMethod:
        return "snippet"


__all__ = ["GenericAdapter", "PlatformAdapter"]
