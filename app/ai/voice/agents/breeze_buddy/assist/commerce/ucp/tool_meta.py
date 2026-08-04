"""Commerce tool metadata — annotations + deterministic verifiers.

The flavor half of ``chat/tool_annotations.py`` and
``chat/verification.py`` for the UCP tool surface. Lazy-loaded with the
rest of the flavor (``schemas.py`` calls :func:`register_commerce_tool_meta`
alongside the step-label registration), so any commerce-enabled process
gets the safety metadata with no extra import hook.

Annotations: catalog/cart READS are ``read_only`` (parallel-safe fan-out
for multi-search turns); cart MUTATIONS are ``idempotent`` (each dispatch
carries the per-turn idempotency hash injected by the session-state
policy) — retried safely but always serialized.

Verifiers: pure post-condition CODE checks over (args, post-pipeline
result) — the reliability mechanism that catches silent partial failures
(a cart update that quietly dropped a line, a stock-capped quantity)
before the model narrates success or a component renders wrong data.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.steps.verification import (
    register_tool_verifier,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import (
    register_tool_annotations,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.result_annotators import (
    register_result_annotator,
)

# Same envelope helper the reducer engine / binding store / intents use.
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _unwrap_tool_payload,
)


def _verify_search_catalog(args: Dict[str, Any], result: Any) -> Optional[str]:
    """A successful search must carry a ``products`` array (possibly
    empty). Anything else means the upstream shape changed — better a
    typed error the model can react to than a hydration mystery later."""
    payload = _unwrap_tool_payload(result)
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        return "search result carries no products array"
    return None


def _requested_lines(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    cart = args.get("cart")
    if not isinstance(cart, dict):
        return []
    lines = cart.get("line_items")
    if not isinstance(lines, list):
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        item = line.get("item")
        variant = item.get("id") if isinstance(item, dict) else None
        qty = line.get("quantity")
        if isinstance(variant, str) and isinstance(qty, int):
            out.append({"id": variant, "qty": qty})
    return out


def _result_quantities(result: Any) -> Dict[str, int]:
    payload = _unwrap_tool_payload(result)
    lines = payload.get("line_items") if isinstance(payload, dict) else None
    out: Dict[str, int] = {}
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        item = line.get("item")
        variant = item.get("id") if isinstance(item, dict) else None
        qty = line.get("quantity")
        if isinstance(variant, str) and isinstance(qty, int):
            out[variant] = out.get(variant, 0) + qty
    return out


def _verify_cart_mutation(args: Dict[str, Any], result: Any) -> Optional[str]:
    """Every requested line must appear in the returned cart with the
    requested quantity (qty 0 = removal must be absent). Catches the
    silent partial-apply class: stock caps, dropped lines, upstream
    truncation — the model gets a precise diff to act on."""
    requested = _requested_lines(args)
    if not requested:
        return None  # free-form call — nothing to post-condition
    actual = _result_quantities(result)
    for want in requested:
        got = actual.get(want["id"], 0)
        if got != want["qty"]:
            return (
                f"cart update did not fully apply: requested quantity "
                f"{want['qty']} for {want['id']}, cart shows {got}"
            )
    return None


def register_commerce_tool_meta() -> None:
    """Register annotations + verifiers for the UCP tool surface.

    Idempotent — same values / same function objects on re-import.
    """
    register_tool_annotations(
        {
            "search_catalog": "read_only",
            "lookup_catalog": "read_only",
            "get_product": "read_only",
            "get_cart": "read_only",
            "create_cart": "idempotent",
            "update_cart": "idempotent",
        }
    )
    register_tool_verifier("search_catalog", _verify_search_catalog)
    register_tool_verifier("create_cart", _verify_cart_mutation)
    register_tool_verifier("update_cart", _verify_cart_mutation)
    # Baseline search annotation (RFC-003 baseline mode): matched_via /
    # matched_variant stamped from THIS turn's live results — lazy import
    # keeps the annotator's regex tables out of non-commerce processes.
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.annotator import (
        annotate_search_result,
    )

    register_result_annotator("search_catalog", annotate_search_result)


__all__ = ["register_commerce_tool_meta"]
