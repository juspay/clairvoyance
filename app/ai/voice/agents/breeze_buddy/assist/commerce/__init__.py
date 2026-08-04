"""Commerce flavor (Component Catalog v2, RFC-001) — the lazy-load entry.

Structure follows the protocol/platform split:

- ``ucp/`` — the protocol layer, and the only layer with opinions about
  commerce. Speaks Universal Commerce Protocol: tool roles (create_cart /
  update_cart / get_cart / get_product / search_catalog, all
  template-overridable via ``configurations.ui_intents.tools``), the wire
  projections and component schemas, the typed ui-intent policy table, the
  UI copy, step labels and tool annotations. No module here names a
  platform.
- ``connectors/`` — one package per platform that serves UCP (``shopify``
  today). A connector only ever registers into ``ucp/hooks.py`` and is
  never imported by the protocol layer, so adding a platform means adding
  a package, never editing UCP code. With zero connectors loaded the
  flavor still works — the hooks fall through to pure-UCP behavior.

Importing THIS module is what registers the flavor: the UI catalog
primitives, the intent policies, the render_ui pack, the selector
transform, the step summarizer, the tool annotations, and then every
connector. Both lazy registries (``ui_catalog.LAZY_GROUPS`` and
``intents.router.FLAVOR_INTENT_MODULES``) point here, so a commerce
template gets the whole flavor consistently no matter which surface
touches it first.

Import order between the two is irrelevant (and isort owns it anyway):
the hook lists are read at CALL time, never at import time, so a
connector registering before or after the protocol modules load makes no
difference.
"""

from __future__ import annotations

# Platform connectors — register into the ucp hooks on import.
from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors import (  # noqa: F401
    shopify,
)

# Protocol layer — schemas pulls in render_ui / ui_prompt / step_labels /
# tool_meta; intents pulls in media / upsell.
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import (  # noqa: F401
    intents,
    schemas,
)

__all__ = ["schemas", "intents", "shopify"]
