"""Post-add cart upsell — the "Goes well with" carousel (2026-07-31).

Fires as the ``add_to_cart`` policy's ``followup`` after the CartView is
already on the wire, so the recommendation costs the shopper no perceived
latency. Zero merchant config, by design:

- **What to recommend** — a micro-LLM call (same pooled Gemini service the
  chat turns use, minimal thinking, strict JSON out) proposes 1-2
  complement CATEGORIES from the live cart contents. This is the one
  decision that needs world knowledge ("top + leggings in cart → socks")
  and the one that killed every static approach: platform-native recs were
  probed live and rejected (Shopify ``related`` returns substitutes — more
  leggings after adding leggings; ``complementary`` is empty unless the
  merchant hand-curates pairings), and per-merchant complement maps don't
  scale (user directive: no dictionaries).
- **Validation** — the UCP catalog search itself: a category the store
  doesn't sell returns zero rows (probed: "skateboards" → 0, no fuzzy
  junk), so an LLM miss degrades to silence, never to junk cards.
- **Size intelligence** — deterministic: candidates keep the shopper's
  just-added size when they have a matching size axis ("XL" leggings →
  only tops available in XL survive; a product whose size axis matches but
  has no stock in that size is DROPPED), and the matching variant is
  stamped via the grid's ``items[].feature_variant`` selector so the
  card's Add is one tap in the right size (the existing A6
  featured_variant_id continuity — no picker detour).

Everything here is fail-open: any error or timeout yields ``None`` (no
upsell block) — a recommendation must never fail the mutation turn.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from app.core.logger import logger

# Overall wall-clock budget for the whole followup (LLM pick + search +
# resolve); the picker gets its own tighter slice. Generous on purpose —
# the cart is already rendered while this runs.
_UPSELL_TIMEOUT_S = 10.0
_PICK_TIMEOUT_S = 5.0
_MAX_ITEMS = 6

_PICK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        }
    },
    "required": ["queries"],
}

# Sentinel: candidate has the shopper's size axis but no stock in their
# size — recommending it would dead-end at an unbuyable size.
_DROP = object()


def _line_title(line: Dict[str, Any]) -> Optional[str]:
    item = line.get("item")
    if isinstance(item, dict) and isinstance(item.get("title"), str):
        return item["title"]
    return None


def _added_line_title(lines: List[Dict[str, Any]], variant_id: str) -> Optional[str]:
    for line in lines:
        item = line.get("item")
        if isinstance(item, dict) and item.get("id") == variant_id:
            title = item.get("title")
            if isinstance(title, str):
                return title
    return None


def _title_tokens(title: str) -> set:
    return {tok.lower() for tok in re.split(r"[^A-Za-z0-9]+", title) if tok}


def _is_size_axis(name: Any) -> bool:
    return isinstance(name, str) and "size" in name.lower()


def match_size_variant(product: Dict[str, Any], added_tokens: set) -> Any:
    """Size-continuity filter for one candidate product.

    Returns the available variant id matching the shopper's size token
    (→ ``feature_variant`` stamp), ``None`` when the product has no
    comparable size axis (no size options at all, or a disjoint vocabulary
    like sock sizing — keep it, the picker handles the choice), or the
    module ``_DROP`` sentinel when the size axis matches the shopper's
    size but that size has no available variant.
    """
    options = product.get("options")
    size_axis = next(
        (
            o
            for o in (options if isinstance(options, list) else [])
            if isinstance(o, dict) and _is_size_axis(o.get("name"))
        ),
        None,
    )
    if size_axis is None:
        return None
    values = [
        str(v.get("label"))
        for v in (size_axis.get("values") or [])
        if isinstance(v, dict) and v.get("label") is not None
    ]
    match = next((v for v in values if v.lower() in added_tokens), None)
    if match is None:
        return None

    # Local import — schemas is flavor-internal and cycle-free, but the
    # availability decoder is the single source of truth for every
    # encoding the live UCP ships (nested {available}, {state}, bare bool).
    from app.ai.voice.agents.breeze_buddy.assist.commerce.schemas import (
        _variant_available,
    )

    variants = product.get("variants")
    for variant in variants if isinstance(variants, list) else []:
        if not isinstance(variant, dict) or not isinstance(variant.get("id"), str):
            continue
        vopts = variant.get("options")
        has_size = any(
            isinstance(o, dict)
            and _is_size_axis(o.get("name"))
            and str(o.get("label", "")).lower() == match.lower()
            for o in (vopts if isinstance(vopts, list) else [])
        )
        if has_size and _variant_available(variant):
            return variant["id"]
    return _DROP


def build_upsell_selectors(
    products: List[Any],
    *,
    added_tokens: set,
    cart_titles: List[str],
    max_items: int = _MAX_ITEMS,
) -> List[Dict[str, str]]:
    """The deterministic curation pass over raw search candidates →
    ``ProductGrid.items`` selectors (id + optional feature_variant).

    Drops: malformed entries, products already in the cart (candidate
    title prefixing a cart line title — line titles carry the variant
    suffix), and size-axis matches with no stock in the shopper's size.
    """
    lowered_cart = [t.lower() for t in cart_titles]
    selectors: List[Dict[str, str]] = []
    for product in products:
        if not isinstance(product, dict) or not isinstance(product.get("id"), str):
            continue
        title = product.get("title")
        if isinstance(title, str) and title:
            if any(ct.startswith(title.lower()) for ct in lowered_cart):
                continue
        feature = match_size_variant(product, added_tokens)
        if feature is _DROP:
            continue
        entry: Dict[str, str] = {"id": product["id"]}
        if isinstance(feature, str):
            entry["feature_variant"] = feature
        selectors.append(entry)
        if len(selectors) >= max_items:
            break
    return selectors


async def _pick_complement_queries(
    template: Any, cart_titles: List[str], added_title: str
) -> List[str]:
    """One-shot Gemini call: cart contents in, 1-2 complement category
    queries out (strict JSON schema, minimal thinking). Reuses the pooled
    chat service — no new client, no new credentials path."""
    from google.genai.types import GenerateContentConfig

    from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
        resolve_llm_configuration,
    )
    from app.ai.voice.agents.breeze_buddy.llm import get_llm_service

    service = await get_llm_service(resolve_llm_configuration(template), pooled=True)
    model = service._settings.model
    # Gemini-only micro-call: the google-genai client is the one with
    # `.aio` (the service union also covers Anthropic/OpenAI engines —
    # those templates simply skip the upsell rather than growing three
    # provider one-shot paths for a decoration).
    client: Any = service._client
    if not isinstance(model, str) or not hasattr(client, "aio"):
        return []

    lines = "\n".join(
        f"- {t}" + (" (just added)" if t == added_title else "") for t in cart_titles
    )
    prompt = (
        "You are a shopping assistant for an online store. A shopper just "
        "added an item to their cart.\n\n"
        f"Cart contents:\n{lines}\n\n"
        "Suggest 1-2 SHORT product-category search queries (1-3 words "
        'each, e.g. "socks" or "sports bra") for COMPLEMENTARY products '
        "that would complete this shopper's outfit or order. Never suggest "
        "a category already covered by the cart. Prefer the most natural "
        "next purchase.\n\n"
        'Return JSON: {"queries": [...]}'
    )
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_PICK_SCHEMA,
                thinking_config={"thinking_level": "minimal"},
                temperature=0.2,
            ),
        ),
        timeout=_PICK_TIMEOUT_S,
    )
    try:
        data = json.loads(response.text or "{}")
    except (TypeError, ValueError):
        return []
    queries = data.get("queries")
    if not isinstance(queries, list):
        return []
    return [q.strip() for q in queries if isinstance(q, str) and q.strip()][:2]


async def _run(
    agent: Any,
    prep: Any,
    node: Dict[str, Any],
    parsed: Any,
    turn_id: str,
    final_tool: str,
    final_result: Any,
) -> Optional[Dict[str, Any]]:
    # Runtime imports from the sibling module: intents.py imports THIS
    # module at load time to wire the policy, so upsell must not import
    # intents at module scope (cycle). By call time it's fully loaded.
    from app.ai.voice.agents.breeze_buddy.assist.commerce.intents import (
        AddToCartPayload,
        _cart_lines,
        _unwrap_tool_payload,
        resolve_cart_config,
    )
    from app.ai.voice.agents.breeze_buddy.chat.intents.router import run_persisted_tool
    from app.ai.voice.agents.breeze_buddy.chat.ui.binding import resolve_show_op

    payload = parsed.payload
    if not isinstance(payload, AddToCartPayload):
        return None

    lines = _cart_lines(_unwrap_tool_payload(final_result))
    cart_titles = [t for t in (_line_title(ln) for ln in lines) if t]
    added_title = _added_line_title(lines, payload.variant_id)
    if not cart_titles or added_title is None:
        return None

    queries = await _pick_complement_queries(agent.template, cart_titles, added_title)
    if not queries:
        return None

    cfg = resolve_cart_config(agent.template)
    added_tokens = _title_tokens(added_title)
    for query in queries:
        # The search exchange persists like any other direct dispatch (the
        # LLM must know what's on screen when the shopper says "add the
        # socks"), but its function_call events are NOT streamed — the
        # upsell should feel ambient, not like a running tool step.
        _events, result = await run_persisted_tool(
            agent,
            tool_name=cfg.search,
            args={"catalog": {"query": query}},
            node=node,
            prep=prep,
            turn_id=turn_id,
        )
        unwrapped = _unwrap_tool_payload(result)
        products = unwrapped.get("products") if isinstance(unwrapped, dict) else None
        if not isinstance(products, list) or not products:
            continue
        selectors = build_upsell_selectors(
            products, added_tokens=added_tokens, cart_titles=cart_titles
        )
        if not selectors:
            continue
        show_op = {
            # id MUST be "root": every widget ui block hosts exactly one op
            # tree anchored at id "root" (ui_state drops any other
            # parentless add — live bug 2026-07-31, invisible upsell). The
            # SDK store routes upsell-marked ops into their OWN block
            # (appendUiOp isUpsellGridAdd branch), so this root never
            # collides with the CartView's root from the same turn.
            "op": "show",
            "id": "root",
            "component": "ProductGrid",
            "bind": {"products": f"$tool:{cfg.search}#/products"},
            "props": {
                "layout": "carousel",
                "max_items": _MAX_ITEMS,
                "items": selectors,
            },
        }
        resolved = resolve_show_op(show_op, agent.binding_store, agent.ui_allowlist)
        if resolved.op is None:
            logger.warning(
                f"cart_upsell: grid resolve dropped ({resolved.error}) — skipping"
            )
            return None
        op = resolved.op
        # Post-resolve server stamps (never part of the LLM-facing schema):
        # the heading the widget renders above the carousel, and the marker
        # the store's cart-family sweep keys on. Persisted verbatim in
        # ui_blocks, so resume sweeps identically.
        props = op.setdefault("props", {})
        # "✨ Picked for you": the sparkle is the de-facto AI signifier and
        # "for you" carries the personalization — without the clinical
        # "AI-recommended" phrasing (user spec 2026-07-31).
        props["heading"] = "✨ Picked for you"
        props["context"] = "cart_upsell"
        return op
    return None


async def run_cart_upsell(
    agent: Any,
    prep: Any,
    node: Dict[str, Any],
    parsed: Any,
    turn_id: str,
    final_tool: str,
    final_result: Any,
) -> Optional[Dict[str, Any]]:
    """``IntentPolicy.followup`` entry — see module docstring. Returns the
    resolved, stamped ProductGrid op or ``None``; never raises."""
    try:
        return await asyncio.wait_for(
            _run(agent, prep, node, parsed, turn_id, final_tool, final_result),
            timeout=_UPSELL_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — recommendations are strictly best-effort
        logger.exception("cart_upsell: followup failed — no upsell block")
        return None


__all__ = ["run_cart_upsell", "build_upsell_selectors", "match_size_variant"]
