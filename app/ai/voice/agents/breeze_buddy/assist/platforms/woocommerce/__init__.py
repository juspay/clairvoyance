"""WooCommerce platform package. Our MCP endpoint (``tools.py``) serves the
catalog and cart; ``wismo/`` looks up orders.

Registration is a side effect of import: importing this package registers the
order-status builtin (``wismo/handlers.py``). ``assist/commerce/__init__.py``
imports this package next to ``shopify``.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo import (  # noqa: F401
    handlers,
)

__all__ = ["handlers"]
