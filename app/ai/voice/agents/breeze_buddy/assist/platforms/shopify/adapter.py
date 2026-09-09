"""ShopifyAdapter — every build-time Shopify fact, in one place.

Signals, the permanent-domain identity rule, the host-app tenancy table,
the prompt sections that only make sense with the storefront MCP, the MCP
server names, the cart-state config entries, the customer-token payload
key, the blueprint requirements, the mirror policy and the install method.
Research fetchers (``sources.py``) arrive with the engine's research stage.
"""

from __future__ import annotations

from typing import FrozenSet, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    InstallMethod,
    MirrorPolicy,
    Signal,
    SiteProfile,
    SiteResearch,
    TenantIdentity,
    ToolBinding,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import (
    LegacyMarkers,
    platform_sections,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import GenericAdapter
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.tenancy import (
    assist_tenant,
)

# (kind, pattern, weight) — verified live on hustleculture.co.in 2026-09-08.
SIGNALS = (
    ("script_src", "cdn.shopify.com", 4.0),
    ("cookie_key", "_shopify_y", 4.0),
    ("cookie_key", "_shopify_s", 3.0),
    ("js_literal", "Shopify.theme", 4.0),
    ("js_global", "cdn.shopify.com", 4.0),
    ("js_global", "/cdn/shop/", 3.0),
    ("header", "powered-by: shopify", 4.0),
    ("meta", "shopify-digital-wallet", 3.0),
)
CONFIDENCE_DENOMINATOR = 10.0

# The fleet's live templates name the storefront MCP server
# ``shopify-storefront-ucp``; the first blueprint used the short form. Both
# point at ``https://{shop_url}/api/ucp/mcp`` and are treated as the same.
MCP_SERVER_NAME = "shopify-storefront-ucp"
MCP_SERVER_NAMES: FrozenSet[str] = frozenset({"shopify-storefront", MCP_SERVER_NAME})
MCP_URL = "https://{shop_url}/api/ucp/mcp"
OPERATING_SECTION = "shopify_operating_section"
# The marker pair the first blueprints shipped with; the engine reads it as
# ``{{#platform_section:shopify}}…{{/platform_section}}``.
LEGACY_SECTION_START = "{{#shopify_operating_section}}"
LEGACY_SECTION_END = "{{/shopify_operating_section}}"
# Cart state plumbing that only means something next to the UCP tools.
TOOL_CONFIG_KEYS: Tuple[str, ...] = (
    "state_reducers",
    "tool_arg_injection",
    "client_context",
)
# Payload keys the storefront session may carry beyond the generic ``shop_url``.
PAYLOAD_KEYS: Tuple[str, ...] = ("shopify_customer_token",)
PERMANENT_DOMAIN_SUFFIX = ".myshopify.com"


class ShopifyAdapter(GenericAdapter):
    id = "shopify"
    request_platform = "shopify"
    # A Shopify store is a shop: the commerce vertical, always.
    vertical: Optional[str] = "commerce"
    # The two Shopify apps that install Assist (see tenancy.py for the namespaces).
    host_apps: Tuple[str, ...] = ("breeze-buddy", "buddy-assist")

    def classify(self, signals: Sequence[Signal]) -> float:
        score = 0.0
        for signal in signals:
            for kind, pattern, weight in SIGNALS:
                if signal.kind == kind and pattern.lower() in signal.pattern.lower():
                    score += weight
                    break
        return score / CONFIDENCE_DENOMINATOR

    def identity(self, profile: SiteProfile) -> TenantIdentity:
        host = (urlsplit(profile.final_url or profile.url).hostname or "").lower()
        # ``Shopify.shop`` in the storefront HTML is the permanent
        # ``*.myshopify.com`` domain — the identity nautilus mints, so a custom
        # domain never needs a second lookup to reach the tenant.
        permanent = (
            profile.inline_literals.get("Shopify.shop") or ""
        ).strip().lower() or None
        return TenantIdentity(
            platform=self.id, canonical_host=host, permanent_host=permanent
        )

    def tenant(self, host_app: str, merchant_domain: str) -> Tuple[str, str]:
        if host_app not in self.host_apps:
            raise ValueError(f"host app {host_app!r} is not a Shopify app")
        return assist_tenant(host_app, merchant_domain)  # type: ignore[arg-type]

    def store_name(self, merchant_domain: str) -> str:
        return merchant_domain.removesuffix(PERMANENT_DOMAIN_SUFFIX)

    def legacy_section_markers(self) -> LegacyMarkers:
        return {LEGACY_SECTION_START: (self.id, LEGACY_SECTION_END)}

    def validate_blueprint(
        self, prompt: str, configurations: Mapping[str, object]
    ) -> List[str]:
        problems: List[str] = []
        try:
            sections = platform_sections(prompt, self.legacy_section_markers())
        except ValueError as exc:
            return [f"invalid platform section: {exc}"]
        if not any(section.platform == self.id for section in sections):
            problems.append("no Shopify section in the operating block")
        mcp = configurations.get("mcp") or {}
        servers = list((mcp.get("servers") if isinstance(mcp, Mapping) else None) or [])
        named = [
            s
            for s in servers
            if isinstance(s, Mapping) and s.get("name") in MCP_SERVER_NAMES
        ]
        if len(named) != 1:
            problems.append("must contain exactly one Shopify storefront MCP server")
        if not configurations.get("state_reducers") or not configurations.get(
            "tool_arg_injection"
        ):
            problems.append("missing Shopify cart state configuration")
        return problems

    def mcp_server_names(self) -> FrozenSet[str]:
        return MCP_SERVER_NAMES

    def tool_config_keys(self) -> Tuple[str, ...]:
        return TOOL_CONFIG_KEYS

    def payload_keys(self) -> Tuple[str, ...]:
        return PAYLOAD_KEYS

    def tools(
        self, identity: TenantIdentity, research: SiteResearch
    ) -> List[ToolBinding]:
        return [ToolBinding(kind="mcp", name=MCP_SERVER_NAME, url=MCP_URL)]

    def extra_origins(
        self, identity: TenantIdentity, research: SiteResearch
    ) -> List[str]:
        origins = list(research.extra_origins)
        if identity.permanent_host:
            origins.append(f"https://{identity.permanent_host}")
        return list(dict.fromkeys(origins))

    def mirror_policy(self) -> MirrorPolicy:
        return MirrorPolicy(
            blocked_paths=[
                "/checkout",
                "/checkouts/",
                "/account",
                "/password",
                "/admin",
            ],
            allowed_xhr=[
                "/cart.js",
                "/cart/add.js",
                "/cart/change.js",
                "/cart/update.js",
                "/search/suggest.json",
                "/recommendations/products.json",
                "/products/",
                "/?sections=",
                "/?section_id=",
            ],
            cdn_hosts=["cdn.shopify.com", "fonts.shopifycdn.com"],
            handoff="permalink",
            section_probe=True,
        )

    def install(self) -> InstallMethod:
        return "theme_embed"


adapter = ShopifyAdapter()

__all__ = [
    "LEGACY_SECTION_END",
    "LEGACY_SECTION_START",
    "MCP_SERVER_NAME",
    "MCP_SERVER_NAMES",
    "MCP_URL",
    "OPERATING_SECTION",
    "PAYLOAD_KEYS",
    "SIGNALS",
    "TOOL_CONFIG_KEYS",
    "ShopifyAdapter",
    "adapter",
]
