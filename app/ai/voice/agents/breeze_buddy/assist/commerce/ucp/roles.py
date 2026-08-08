"""UCP tool roles — what this flavor calls its tools, and how a template
rebinds them.

A ROLE is the flavor's name for a job ("search the catalog"); the TOOL is
whatever the merchant's gateway actually exposes for it. The UCP defaults
below are what the pilot templates speak, and
``configurations.ui_intents.tools`` overrides any of them per template.

Everything the flavor registers into an engine registry — step labels,
annotations, verifiers, result annotators — is keyed by these role names,
not by tool names, and the engine resolves them through the binding here
(see ``chat/flavors.py``). That is what keeps a merchant whose gateway
calls it ``find_products`` from silently losing the cart verifier, the
read-only annotation and the step label that ``search_catalog`` gets.

This module deliberately imports nothing from the rest of the flavor: it
is the one piece both the intent driver and the registration hook need,
and a dependency in either direction would make the lazy-load order
matter.
"""

from __future__ import annotations

from typing import Any, Dict

# Role → default tool name. The keys ARE the registry keys used across the
# flavor; the values are the Stage-A UCP surface.
ROLE_SEARCH = "search"
ROLE_GET_PRODUCT = "get_product"
ROLE_CREATE_CART = "create_cart"
ROLE_UPDATE_CART = "update_cart"
ROLE_GET_CART = "get_cart"

DEFAULT_TOOLS: Dict[str, str] = {
    ROLE_SEARCH: "search_catalog",
    ROLE_GET_PRODUCT: "get_product",
    ROLE_CREATE_CART: "create_cart",
    ROLE_UPDATE_CART: "update_cart",
    ROLE_GET_CART: "get_cart",
}


def resolve_role_map(template: Any) -> Dict[str, str]:
    """``{role: tool_name}`` for ``template`` — the UCP defaults with the
    template's ``configurations.ui_intents.tools`` overlaid.

    Unknown keys in the template block are ignored: it may carry roles for
    another flavor sharing the same template.
    """
    configurations = getattr(template, "configurations", None)
    ui_intents = getattr(configurations, "ui_intents", None)
    overrides = getattr(ui_intents, "tools", None) or {}
    if not isinstance(overrides, dict):
        return dict(DEFAULT_TOOLS)
    return {
        role: overrides.get(role) or default for role, default in DEFAULT_TOOLS.items()
    }


__all__ = [
    "DEFAULT_TOOLS",
    "ROLE_CREATE_CART",
    "ROLE_GET_CART",
    "ROLE_GET_PRODUCT",
    "ROLE_SEARCH",
    "ROLE_UPDATE_CART",
    "resolve_role_map",
]
