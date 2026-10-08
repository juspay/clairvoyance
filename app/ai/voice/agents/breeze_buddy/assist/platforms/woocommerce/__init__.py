"""WooCommerce connector. Our MCP endpoint (``tools.py``) serves the catalog
and cart; this package registers one hook, the order lookup."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    register_order_lookup,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.order_tracking import (
    lookup_order,
)

# The name a template uses in ``flavor.<protocol>.connectors``.
CONNECTOR_NAME = "woocommerce"

register_order_lookup(CONNECTOR_NAME, lookup_order)

__all__ = ["CONNECTOR_NAME", "lookup_order"]
