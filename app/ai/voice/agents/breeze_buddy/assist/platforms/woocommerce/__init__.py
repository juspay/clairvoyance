"""WooCommerce connector: an in-process UCP gateway (``gateway.py``).

Templates point their commerce server at ``local://woocommerce/<store host>``.
It registers no projection hooks. Its templates name it in
``flavor.ucp.connectors``, which keeps other connectors' hooks off its data.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.gateway import (
    call_tool,
)
from app.ai.voice.agents.breeze_buddy.mcp.local_gateway import register_local_gateway

# The name a template uses in ``flavor.<protocol>.connectors`` and in its
# server URL, ``local://woocommerce/<store host>``.
CONNECTOR_NAME = "woocommerce"

register_local_gateway(CONNECTOR_NAME, call_tool)

__all__ = ["CONNECTOR_NAME", "call_tool"]
