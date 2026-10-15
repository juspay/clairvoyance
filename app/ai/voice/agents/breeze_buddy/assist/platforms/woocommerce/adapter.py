"""WooCommerceAdapter — the build-time WooCommerce facts onboarding needs.

Signals, the tool server the blueprint carries for these stores, the
connector the built template names, and the store's item folders. The
tools themselves are served at ``/mcp/woocommerce/<host>`` (``tools.py``).
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.classify.signals import (
    signal_matches,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal
from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import (
    SHOP_DOMAIN_PLACEHOLDER,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import GenericAdapter

# (kind, pattern, weight) — what WooCommerce prints in its storefront scripts.
# Every store checked prints its ``woocommerce-no-js`` body-class swap; a theme
# may drop the other settings (nutribullet.com prints only the add-to-cart one).
SIGNALS = (
    ("js_global", "woocommerce-no-js", 4.0),
    ("js_global", "woocommerce_params", 4.0),
    ("js_global", "wc_add_to_cart_params", 3.0),
    ("js_global", "wc_cart_fragments_params", 3.0),
)
CONFIDENCE_DENOMINATOR = 10.0

# The blueprint's tool server for these stores. Its URL names the store host
# through ``{{shop_domain}}``, so the build writes the host literally: a
# ``{shop_url}`` store is never answered in process.
MCP_SERVER_NAME = "woocommerce-store"
MCP_URL_SUFFIX = f"/mcp/woocommerce/{SHOP_DOMAIN_PLACEHOLDER}"
# Names this connector, so Shopify-only data fixes never run on its data; the
# blueprint's features (upsell, ...) are kept.
CONFIG_OVERRIDES: Dict[str, Any] = {"flavor": {"ucp": {"connectors": ["woocommerce"]}}}


class WooCommerceAdapter(GenericAdapter):
    id = "woocommerce"
    request_platform = "woocommerce"
    vertical: Optional[str] = "commerce"

    def probe_markers(self) -> Tuple[str, ...]:
        return tuple(pattern for kind, pattern, _ in SIGNALS if kind == "js_global")

    def classify(self, signals: Sequence[Signal]) -> float:
        score = 0.0
        for signal in signals:
            for kind, pattern, weight in SIGNALS:
                if signal_matches(signal, kind, pattern):
                    score += weight
                    break
        return score / CONFIDENCE_DENOMINATOR

    def validate_blueprint(
        self, prompt: str, configurations: Mapping[str, object]
    ) -> List[str]:
        mcp = configurations.get("mcp") or {}
        servers = list((mcp.get("servers") if isinstance(mcp, Mapping) else None) or [])
        named = [
            s
            for s in servers
            if isinstance(s, Mapping) and s.get("name") == MCP_SERVER_NAME
        ]
        if len(named) != 1:
            return ["must contain exactly one WooCommerce tool server"]
        if not str(named[0].get("url") or "").endswith(MCP_URL_SUFFIX):
            return [f"WooCommerce tool server URL must end with {MCP_URL_SUFFIX}"]
        return []

    def mcp_server_names(self) -> FrozenSet[str]:
        return frozenset({MCP_SERVER_NAME})

    def config_overrides(self) -> Mapping[str, Any]:
        return CONFIG_OVERRIDES

    def folders_to_skip(self) -> Tuple[str, ...]:
        return ("product", "product-category", "product-tag")


adapter = WooCommerceAdapter()

__all__ = [
    "MCP_SERVER_NAME",
    "MCP_URL_SUFFIX",
    "SIGNALS",
    "WooCommerceAdapter",
    "adapter",
]
