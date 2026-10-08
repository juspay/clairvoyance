"""The six commerce tools for a WooCommerce store, by name.

``call_tool`` answers ``/mcp/woocommerce/<store host>`` (``mcp/in_process.py``).
A failure the model can act on is a ``ToolError`` with a readable message.
"""

from __future__ import annotations

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import (
    cart,
    catalog,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.store_api import (
    StoreError,
)
from app.ai.voice.agents.breeze_buddy.mcp.server import ToolError
from app.core.logger import logger
from app.core.security.ssrf import SSRFError

_TOOLS = {
    "search_catalog": catalog.search_catalog,
    "lookup_catalog": catalog.lookup_catalog,
    "get_product": catalog.get_product,
    "create_cart": cart.set_cart,
    "update_cart": cart.set_cart,
    "get_cart": cart.get_cart,
}


def store_api_url(host: str) -> str:
    return f"https://{host}/wp-json/wc/store/v1"


async def call_tool(base: str, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run one tool against the Store API at ``base``."""
    tool = _TOOLS.get(tool_name)
    if tool is None:
        raise ToolError(f"Unknown tool {tool_name!r}.")
    try:
        return await tool(base, args)
    except StoreError as e:
        raise ToolError(str(e)) from e
    except SSRFError as e:
        # Logged in full; the model never sees the resolved address.
        logger.warning(f"[woocommerce] egress refused for {base!r}: {e}")
        raise ToolError("The store address is not reachable from here.") from e
    except (TypeError, ValueError, AttributeError) as e:
        # An argument the model sent in the wrong shape (a cursor, price or
        # quantity that is not a number; an object that is not one).
        raise ToolError(f"Invalid tool arguments: {e}") from e
