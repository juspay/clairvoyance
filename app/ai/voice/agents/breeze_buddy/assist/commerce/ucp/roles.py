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

from typing import Any, Dict, Optional

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


def pick_checkout_url(
    configured: Any,
    payload_url: Any,
    state_url: Any,
) -> Optional[str]:
    """The CartView checkout button's destination — ONE precedence, for
    every path that builds that button.

    ``configured`` (``ui_intents.urls.checkout_page``) wins: a merchant
    who names a page means it, and that page is typically their own
    storefront cart, which the CartView cookie sync exists to populate.
    Then the cart payload's ``continue_url``, then the reducer-state
    fallback (the ``state_keys.checkout_url`` role). ``None`` when there
    is nothing to point at — the caller renders no button rather than a
    dead one.

    Takes RESOLVED candidates rather than the sources, because the two
    callers reach them differently: the DIRECT driver has one tool
    payload plus the agent's state, the render_ui finalizer has a
    BindingStore and bind refs. Only the ORDER is shared — and only the
    order needed sharing: it was duplicated in both, so
    ``urls.checkout_page`` shipped honoured on the DIRECT path and
    ignored on the model-driven one, giving one button two destinations.

    Non-string candidates are skipped rather than returned; template JSON
    is merchant-edited, and a wrong-typed value should fall through to
    the next tier, never become an href.
    """
    for candidate in (configured, payload_url, state_url):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


__all__ = [
    "DEFAULT_TOOLS",
    "ROLE_CREATE_CART",
    "ROLE_GET_CART",
    "ROLE_GET_PRODUCT",
    "ROLE_SEARCH",
    "ROLE_UPDATE_CART",
    "pick_checkout_url",
    "resolve_role_map",
]
