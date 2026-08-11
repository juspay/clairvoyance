"""Commerce render_ui flavor pack (group: commerce).

The engine (``chat/ui/render_ui_tool.py``) owns the render_ui MECHANISM;
everything commerce-shaped about that surface lives here (moved verbatim
out of the engine, 2026-08-05): the LLM-facing arg descriptions that
coach in shop vocabulary (products, carts, checkout links, variant
featuring), the function-response summarizer that knows the commerce
prop shapes (``products`` / ``product`` / ``line_items`` + id/title/
variant referents), and the post-hydration projection policy (layout by
product count; the CartView checkout button stamped from the bound cart
payload's ``continue_url`` with the ``ui_intents``-role state fallback).

Lazy-loaded with the rest of the flavor: ``schemas.py`` — the module
``ui_catalog.ensure_group_loaded("commerce")`` imports — calls
:func:`register_commerce_render_ui_pack` alongside the component
registration, before any schema build or render executes. Deliberately
import-free of ``intents.py`` (the ``ui_intents`` role read is inlined,
mirroring ``resolve_cart_config`` defaults) so loading the schemas never
drags in the chat intent stack — see ``assist/__init__``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.roles import (
    ROLE_SEARCH,
    pick_checkout_url,
    resolve_role_map,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.step_labels import (
    COMMERCE_GROUP,
)
from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    register_flavor_roles,
    role_key,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
    parse_bind_ref,
    register_selector_transform,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    RenderUiFlavorPack,
    register_render_ui_flavor_pack,
)

# Summary caps — the function response is the model's UI memory (the
# structured replacement for the old ``[ui rendered: …]`` marker); it must
# stay ~50-80 tokens, never a payload echo.
_SUMMARY_ITEMS_CAP = 8
_SUMMARY_TITLE_CAP = 60

_TOOL_DESC = (
    "Render UI for the shopper THIS turn. Call it once. Values are "
    "filled from this turn's tool results — you author selectors "
    "only. When a search returned products, default to showing "
    "them. decision='no_ui' renders nothing (empty results or a "
    "purely conversational reply)."
)
_BIND_DESC = (
    "Data bindings into THIS turn's tool results, e.g. "
    "[{'prop':'products','ref':'$tool:search_catalog#/products'}]. "
    "CartView binds cart_id/line_items/totals/cart_token off "
    "the cart tool result the same way; its checkout button "
    "is automatic — never author it."
)
_ITEMS_DESC = (
    "ProductGrid selection: which bound products to show, in "
    "this order (ids from THIS turn's results). Use when the "
    "shopper asked for specific product(s); omit for "
    "open-ended browsing. feature_variant: a variant id from "
    "that product's variants to feature as the card hero "
    "(e.g. the pink one the shopper asked for) — prefer the "
    "search result's matched_variant when present."
)
_QUICK_DESC = (
    "QuickReplies content (only with component='QuickReplies'): 2-5 "
    "short strings — each is exactly what the shopper sees on the "
    "pill AND what comes back as their next message when tapped."
)
_QUICK_RIDER_DESC = (
    "Optional, attachable to ANY call: 2-5 short follow-up "
    "strings shown as tappable pills UNDER your final reply — "
    "each is exactly what the shopper sees AND what comes back "
    "as their next message when tapped. Attach them to the "
    "render_ui call that accompanies your reply; placement is "
    "automatic. Labels <=4 words; never duplicate an action "
    "already available on UI rendered this turn (cards already "
    "carry Add to cart / View)."
)
_LINK_DESC = (
    "LinkButton content (only with component='LinkButton'): a single "
    "link CTA for link-only answers (e.g. 'just give me the checkout "
    "link'). url must be one of the trusted URLs below or a URL from "
    "THIS turn's tool results — anything else is rejected."
)
_BIND_EXAMPLE = "[{'prop':'products','ref':'$tool:search_catalog#/products'}]"
_LINK_FALLBACK_HINT = "pass the store's configured checkout URL"

_DEFAULT_CHECKOUT_LABEL = "Review and checkout"


def _feature_variant_entry(entry: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
    """Re-derive a product entry's HERO from one of its own tool-sourced
    variant records (RFC-003 §4: "pink card, not black-with-a-footnote").

    Registered as the ``feature_variant`` selector transform (see
    ``binding.register_selector_transform``) — runs per selected entry
    during ``items[]`` selection. Only fields the variant record actually
    carries are rewritten (price / image); everything stays tool-sourced
    by construction — an unknown ``variant_id`` returns the entry
    untouched (fail-open, same posture as unknown selection ids)."""
    variants = entry.get("variants")
    if not isinstance(variants, list):
        return entry
    variant = next(
        (v for v in variants if isinstance(v, dict) and v.get("id") == variant_id),
        None,
    )
    if variant is None:
        return entry
    out = dict(entry)
    out["featured_variant_id"] = variant_id
    price = variant.get("price")
    if price is not None:
        out["price"] = price
    for key in ("image", "image_url", "featured_image"):
        img = variant.get(key)
        if img:
            out["image"] = (
                {"src": img, "alt": out.get("title")} if isinstance(img, str) else img
            )
            break
    return out


def _merge_repeat_grid(
    component: str,
    prev_props: Dict[str, Any],
    new_props: Dict[str, Any],
) -> Optional[tuple]:
    """Repeat-render policy: a SECOND ProductGrid this turn merges
    value-level into the first (works across different searches —
    hydrated values need no bind re-resolution), dedupes on product id,
    caps at 12, restamps layout from the combined count. One combined
    product display per turn, never stacked surfaces. Other components
    return ``None`` — no merge."""
    if component != "ProductGrid":
        return None
    prev_products = [
        p for p in (prev_props.get("products") or []) if isinstance(p, dict)
    ]
    seen_ids = {p.get("id") for p in prev_products}
    extra = [
        p
        for p in (new_props.get("products") or [])
        if isinstance(p, dict) and p.get("id") not in seen_ids
    ]
    merged_products = (prev_products + extra)[:12]
    merged = dict(prev_props)
    merged["products"] = merged_products
    merged["layout"] = "grid" if len(merged_products) <= 2 else "carousel"
    return merged, (
        "combined with this turn's earlier product display — one display per turn"
    )


def _summarize_commerce(
    component: str, props: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Commerce prop shapes → the model's UI memory. ``items`` carries
    id + title (+ featured_variant) so referents like "the green one" and
    follow-up ``items[]`` selections resolve without re-searching. Full
    gid stays authoritative — it's the referent the model reuses in
    items[]/view_product. Returns ``None`` for non-commerce shapes
    (QuickReplies, LinkButton) — the engine's generic summary handles
    them."""
    result: Dict[str, Any] = {"status": "ok", "rendered": component}
    products = props.get("products")
    if isinstance(products, list):
        result["count"] = len(products)
        items: List[Dict[str, Any]] = []
        for entry in products[:_SUMMARY_ITEMS_CAP]:
            if not isinstance(entry, dict):
                continue
            item: Dict[str, Any] = {
                "id": entry.get("id"),
                "title": str(entry.get("title", ""))[:_SUMMARY_TITLE_CAP],
            }
            fv = entry.get("featured_variant_id")
            if fv:
                item["featured_variant"] = fv
            items.append(item)
        result["items"] = items
        return result
    if isinstance(props.get("product"), dict):
        p = props["product"]
        result["items"] = [
            {
                "id": p.get("id"),
                "title": str(p.get("title", ""))[:_SUMMARY_TITLE_CAP],
            }
        ]
        return result
    if isinstance(props.get("line_items"), list):  # CartView
        result["count"] = len(props["line_items"])
        return result
    return None


def _finalize_commerce(
    component: str,
    schema_cls: Any,
    hydrated_props: Dict[str, Any],
    *,
    bind: Dict[str, str],
    store: BindingStore,
    template: Any,
    state_values: Optional[Dict[str, Any]],
) -> None:
    """Post-hydration projection policy — server-authored, never the model.

    Layout is derived from the FINAL hydrated count (post items[]
    selection, post max_items cap): 1-2 products sit side by side; 3+
    scroll as a carousel. The CartView checkout button mirrors the
    DIRECT-intent path exactly: the configured fixed destination
    (``ui_intents.urls.checkout_page``) wins, then the bound cart
    payload's ``continue_url``, then the reducer-state fallback
    (``ui_intents.state_keys.checkout_url`` role); no url anywhere →
    no button."""
    if isinstance(hydrated_props.get("products"), list):
        hydrated_props["layout"] = (
            "grid" if len(hydrated_props["products"]) <= 2 else "carousel"
        )
    if (
        schema_cls is not None
        and "checkout" in schema_cls.model_fields
        and not hydrated_props.get("checkout")
    ):
        ui_intents = getattr(
            getattr(template, "configurations", None), "ui_intents", None
        )
        state_keys = (
            (getattr(ui_intents, "state_keys", None) or {}) if ui_intents else {}
        )
        labels = (getattr(ui_intents, "labels", None) or {}) if ui_intents else {}
        urls = (getattr(ui_intents, "urls", None) or {}) if ui_intents else {}
        payload_url: Optional[Any] = None
        for ref in bind.values():
            parsed = parse_bind_ref(ref)
            if parsed is None:
                continue
            payload = store.resolve(parsed.tool_name, parsed.tool_use_id)
            if isinstance(payload, dict):
                cu = payload.get("continue_url")
                if isinstance(cu, str) and cu:
                    payload_url = cu
                    break
        # Precedence itself lives in ``roles.pick_checkout_url`` — shared
        # with the DIRECT path's ``intents.py::_cart_view_show_op``, which
        # resolves the same three candidates from different sources. It
        # was duplicated, and this path silently lacked the configured
        # tier, so one button had two destinations.
        checkout_url = pick_checkout_url(
            urls.get("checkout_page"),
            payload_url,
            (
                state_values.get(state_keys.get("checkout_url", "checkout_url"))
                if state_values
                else None
            ),
        )
        if checkout_url:
            hydrated_props["checkout"] = {
                "label": labels.get("checkout") or _DEFAULT_CHECKOUT_LABEL,
                "url": checkout_url,
            }


COMMERCE_RENDER_UI_PACK = RenderUiFlavorPack(
    tool_description=_TOOL_DESC,
    bind_description=_BIND_DESC,
    items_description=_ITEMS_DESC,
    quick_replies_description=_QUICK_DESC,
    quick_replies_rider_description=_QUICK_RIDER_DESC,
    link_description=_LINK_DESC,
    bind_example=_BIND_EXAMPLE,
    link_untrusted_fallback_hint=_LINK_FALLBACK_HINT,
    # A ROLE, not a tool name: the engine binds it through the template
    # (see roles.py), so the think-step still fires for a merchant whose
    # gateway calls the search tool something else.
    default_force_after=[role_key(ROLE_SEARCH)],
    summarize=_summarize_commerce,
    finalize_hydrated=_finalize_commerce,
    merge_repeat_render=_merge_repeat_grid,
)


def register_commerce_render_ui_pack() -> None:
    """Register the commerce pack into the render_ui flavor registry, and
    the ``feature_variant`` selector transform into the binding engine.

    Idempotent (same-key overwrite) — safe on re-import.
    """
    register_render_ui_flavor_pack(COMMERCE_GROUP, COMMERCE_RENDER_UI_PACK)
    register_selector_transform(
        COMMERCE_GROUP, "feature_variant", _feature_variant_entry
    )
    # How this flavor's roles bind to tool names on a given template — the
    # table every other registration above is keyed against.
    register_flavor_roles(COMMERCE_GROUP, resolve_role_map)


__all__ = [
    "COMMERCE_RENDER_UI_PACK",
    "register_commerce_render_ui_pack",
]
