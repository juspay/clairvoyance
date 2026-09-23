"""The adapter interface, and the generic adapter every platform extends.

Rule (ASSIST-ENGINE-DESIGN.md §1): the engine works on any public website
with ZERO adapters — ``GenericAdapter`` is that behaviour. A platform adds
by overriding hooks, never by forking a stage. Everything a build stage
used to branch on is a hook here: which prompt sections to keep, which MCP
servers and config entries belong to the platform, which payload keys it
needs, how a host app maps to a tenant, how a blueprint must look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    FrozenSet,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandLook,
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


@dataclass(frozen=True)
class KnownDocument:
    """A document a platform already knows the address, and often the text, of.

    ``body`` empty means the platform named the document but would not produce
    it — the researcher fetches ``url`` itself. ``display_url`` is the same
    document on the merchant's own domain, for showing a person an address
    they recognise rather than a vendor's host.
    """

    kind: str
    title: str
    url: str
    body: Optional[str] = None
    display_url: Optional[str] = None


class PlatformAdapter(Protocol):
    id: str
    request_platform: str
    host_apps: Tuple[str, ...]
    # The vertical this platform serves, or None when any (a plain website).
    vertical: Optional[str]

    def probe_literals(self) -> Tuple[str, ...]: ...

    def probe_markers(self) -> Tuple[str, ...]: ...

    def classify(self, signals: Sequence[Signal]) -> float: ...

    def identity(self, profile: SiteProfile) -> TenantIdentity: ...

    def tenant(self, host_app: str, merchant_domain: str) -> Tuple[str, str]: ...

    def store_name(self, merchant_domain: str) -> str: ...

    async def research(
        self, profile: SiteProfile, budget_seconds: float
    ) -> ResearchDelta: ...

    async def brand(self, profile: SiteProfile) -> Optional[BrandLook]: ...

    def stock_colors(self) -> Tuple[str, ...]: ...

    async def known_documents(
        self, profile: SiteProfile
    ) -> Tuple[KnownDocument, ...]: ...

    def slot_profile(self) -> str: ...

    def known_fields(self, profile: SiteProfile) -> Mapping[str, str]: ...

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

    def probe_literals(self) -> Tuple[str, ...]:
        """Inline-script assignment names the probe should read for us.

        The probe cannot guess which names matter without naming platforms,
        so each adapter asks for its own; the values land in
        ``SiteProfile.inline_literals`` and a ``js_literal`` signal records
        that the assignment was there.
        """
        return ()

    def probe_markers(self) -> Tuple[str, ...]:
        """Substrings whose presence in inline script text is a ``js_global``
        signal — the asset paths and hosts a platform's own scripts mention."""
        return ()

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

    def slot_profile(self) -> str:
        """Which set of sections an assistant for this kind of site is built from.

        A site nobody recognises gets the neutral set, which assumes only that
        a business has customers. An adapter that knows better says so, and the
        name is resolved outside the engine — the engine must not know that a
        vertical exists, let alone which one this is.
        """
        return "generic"

    def known_fields(self, profile: SiteProfile) -> Mapping[str, str]:
        """Field values the platform knows without anyone reading anything.

        A hosted storefront's basket lives at a fixed path, so hoping a
        researcher stumbles across it is worse than asking the adapter that
        already knows. Only ever fills a field research left empty.
        """
        return {}

    async def known_documents(
        self, profile: SiteProfile
    ) -> Tuple["KnownDocument", ...]:
        """Documents this platform can hand over without being asked twice.

        A shortcut, never a substitute. Where a platform publishes a store's
        policies through an API, one call replaces a search — but measured on
        three live stores, one of them returned a single policy in full and
        three empty ones. So this contributes what it has and the researcher
        reads the rest; a caller that treated an answer here as the answer
        would ship those three blank and never notice.
        """
        return ()

    async def brand(self, profile: SiteProfile) -> Optional[BrandLook]:
        """The platform's own record of how this merchant looks.

        Authoritative where a platform keeps one, because the merchant set it
        themselves. A plain website keeps none, so the engine reads the page.
        """
        return None

    def stock_colors(self) -> Tuple[str, ...]:
        """Colours belonging to the platform rather than to any merchant.

        A stock theme's palette and a vendor's own badge both read as confident
        brand colours to anything measuring pixels or stylesheets. Naming them
        here is how the engine tells a merchant's identity from its host's.
        """
        return ()

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


__all__ = ["GenericAdapter", "KnownDocument", "PlatformAdapter"]
