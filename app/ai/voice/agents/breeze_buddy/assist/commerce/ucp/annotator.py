"""Baseline search annotation (RFC-003 §3 baseline mode — NOT OKF).

Code-only matching of the shopper's ask against THIS turn's live UCP search
results: product titles, tags, and variant option values are all in the
payload already, so ``matched_via`` / ``matched_variant`` work for every
merchant with zero sync. Stage ① (alias expansion) and the wider OKF
vocabulary are the Stage-2 opt-in; this annotator is the 90%.

Contract (result_annotators): ADD keys only — a matched product entry gains

    "matched_via":     "exact" | "fuzzy"
    "matched_variant": {"id": …, "title": …}     (when the ask names a
                                                  variant-distinguishing
                                                  token, e.g. "pink")

and the payload gains a compact top-level ``match`` summary. The model
passes ``matched_variant.id`` through as ``items[].feature_variant`` in
``render_ui`` (code decides when the query decided); rendered values stay
tool-sourced — a wrong match can only mis-EMPHASIZE, never mis-render.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _unwrap_tool_payload,
)

# Query noise: intent verbs, filler, price-ask scaffolding. Tokens the
# catalog can't match on and that would dilute exact/fuzzy scoring.
_STOPWORDS = frozenset("""
    a an the me my i you your we our do does have has any some show see want
    need looking look for of in on with and or to under below above over
    around about price priced rs inr rupees than is are it this that please
    buy get find them there available
    """.split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cap matched products annotated per page — the whole page is 10 today;
# annotation on everything would mean the query didn't discriminate.
_MAX_MATCHED = 6


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


def _query_tokens(query: str) -> List[str]:
    return [t for t in _tokens(query) if t not in _STOPWORDS and not t.isdigit()]


def _product_haystack(product: Dict[str, Any]) -> Set[str]:
    parts: List[str] = [str(product.get("title") or "")]
    tags = product.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    ptype = product.get("product_type")
    if isinstance(ptype, str):
        parts.append(ptype)
    out: Set[str] = set()
    for part in parts:
        out.update(_tokens(part))
    return out


def _variant_tokens(variant: Dict[str, Any]) -> Set[str]:
    """Tokens that can distinguish one variant: its title ("S / Pink") plus
    any option values (``options``/``selected_options`` shapes)."""
    parts: List[str] = [str(variant.get("title") or "")]
    for key in ("options", "selected_options", "option_values"):
        val = variant.get(key)
        if isinstance(val, list):
            for entry in val:
                if isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, dict):
                    v = entry.get("value") or entry.get("name")
                    if isinstance(v, str):
                        parts.append(v)
        elif isinstance(val, dict):
            parts.extend(str(v) for v in val.values() if isinstance(v, str))
    out: Set[str] = set()
    for part in parts:
        out.update(_tokens(part))
    return out


def _match_variant(
    product: Dict[str, Any], query_tokens: Set[str]
) -> Optional[Dict[str, Any]]:
    """The variant whose DISTINGUISHING tokens intersect the ask.

    A token shared by every variant of the product (e.g. the product name
    riding each variant title) distinguishes nothing and is ignored — only
    tokens that separate one variant from its siblings count, so "pink"
    picks the pink variant and "shorts" picks none.
    """
    variants = product.get("variants")
    if not isinstance(variants, list) or len(variants) < 2:
        return None
    token_sets: List[Tuple[Dict[str, Any], Set[str]]] = []
    for variant in variants:
        if isinstance(variant, dict) and variant.get("id"):
            token_sets.append((variant, _variant_tokens(variant)))
    if len(token_sets) < 2:
        return None
    common = set.intersection(*(ts for _, ts in token_sets))
    best: Optional[Dict[str, Any]] = None
    best_hits = 0
    for variant, ts in token_sets:
        hits = len((ts - common) & query_tokens)
        if hits > best_hits:
            best, best_hits = variant, hits
    if best is None:
        return None
    return {"id": best.get("id"), "title": best.get("title")}


def annotate_search_result(args: Dict[str, Any], result: Any) -> Any:
    """The registered annotator for ``search_catalog`` (baseline mode)."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return result
    payload = _unwrap_tool_payload(result)
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list) or not products:
        return result

    q_tokens = set(_query_tokens(query))
    if not q_tokens:
        return result

    scored: List[Tuple[int, bool, Dict[str, Any]]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        haystack = _product_haystack(product)
        variant_match = _match_variant(product, q_tokens)
        hay_hits = len(q_tokens & haystack)
        hits = hay_hits + (1 if variant_match else 0)
        if hits == 0:
            continue
        exact = q_tokens <= (
            haystack | (_variant_tokens_all(product) if variant_match else set())
        )
        scored.append((hits, exact, product))
        if variant_match and variant_match.get("id"):
            product["matched_variant"] = variant_match

    if not scored:
        return result
    scored.sort(key=lambda t: (t[1], t[0]), reverse=True)
    top_hits = scored[0][0]
    matched_ids: List[str] = []
    for hits, exact, product in scored[:_MAX_MATCHED]:
        # Annotate the leaders; a lone straggler token match on rank 9 is
        # noise, not a match.
        if hits * 2 < top_hits:
            continue
        product["matched_via"] = "exact" if exact else "fuzzy"
        pid = product.get("id")
        if isinstance(pid, str):
            matched_ids.append(pid)
    if matched_ids and isinstance(payload, dict):
        payload["match"] = {"query": query, "matched_ids": matched_ids}
    return result


def _variant_tokens_all(product: Dict[str, Any]) -> Set[str]:
    variants = product.get("variants")
    out: Set[str] = set()
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                out.update(_variant_tokens(variant))
    return out


__all__ = ["annotate_search_result"]
