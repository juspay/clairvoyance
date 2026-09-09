"""ShopifyAdapter — every build-time Shopify fact, in one place.

Signals, the permanent-domain identity rule, the prompt sections that only
make sense with the storefront MCP, the tool binding, the mirror policy
and the install method. Research fetchers (``sources.py``) arrive with the
engine's research stage.
"""

from __future__ import annotations

from typing import List, Sequence
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    InstallMethod,
    MirrorPolicy,
    Signal,
    SiteProfile,
    StoreResearch,
    TenantIdentity,
    ToolBinding,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import GenericAdapter

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
MCP_SERVER_NAME = "shopify-storefront-ucp"
MCP_URL = "https://{shop_url}/api/ucp/mcp"
OPERATING_SECTION = "shopify_operating_section"


class ShopifyAdapter(GenericAdapter):
    id = "shopify"

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

    def operating_sections(self) -> List[str]:
        return [OPERATING_SECTION]

    def tools(
        self, identity: TenantIdentity, research: StoreResearch
    ) -> List[ToolBinding]:
        return [ToolBinding(kind="mcp", name=MCP_SERVER_NAME, url=MCP_URL)]

    def extra_origins(
        self, identity: TenantIdentity, research: StoreResearch
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
            cart_handoff="permalink",
            section_probe=True,
        )

    def install(self) -> InstallMethod:
        return "theme_embed"


adapter = ShopifyAdapter()

__all__ = [
    "MCP_SERVER_NAME",
    "MCP_URL",
    "OPERATING_SECTION",
    "SIGNALS",
    "ShopifyAdapter",
    "adapter",
]
