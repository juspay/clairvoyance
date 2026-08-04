"""Shopify connector — the only place in this flavor that knows Shopify.

Registration is a side effect of import; the flavor's package __init__
imports this module after the UCP layer is loaded.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors.shopify.media import (
    resolve_gallery,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors.shopify.quirks import (
    repair_flattened_description,
    suppress_default_title_variant,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    register_description_repair,
    register_media_resolver,
    register_variant_normalizer,
)

# The name a template uses in ``flavor.<protocol>.connectors``.
CONNECTOR_NAME = "shopify"

register_media_resolver(CONNECTOR_NAME, resolve_gallery)
register_variant_normalizer(suppress_default_title_variant)
register_description_repair(repair_flattened_description)

__all__ = [
    "CONNECTOR_NAME",
    "resolve_gallery",
    "repair_flattened_description",
    "suppress_default_title_variant",
]
