"""Deterministic tool-result ANNOTATORS (RFC-003 baseline mode).

The transforming sibling of ``chat/verification.py``: after a tool executes
(and BEFORE verification / binding store / reducers / LLM context), its
registered annotators may enrich the result with derived, code-computed
annotations — e.g. the commerce search annotator stamps ``matched_via`` /
``matched_variant`` on products that match the shopper's ask, using ONLY
this turn's live result (titles, tags, variant option values). No stores,
no sync, no embeddings — pure code over the payload in hand.

Contract: annotators are pure and fast (no I/O, no model calls), must only
ADD keys (never mutate existing values — rendered VALUES stay tool-sourced,
RFC-001), and fail open: a raising annotator logs and returns the result
untouched.

Registration is per catalog GROUP and keyed by tool ROLE (see
``chat/flavors.py``): an annotator runs only for sessions whose template
enabled its flavor, and follows the tool the template bound the role to.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    EMPTY_SCOPE,
    FlavorScope,
    scoped_keys,
)
from app.core.logger import logger

# (args, post-pipeline result) → annotated result (same object or enriched copy).
AnnotatorFn = Callable[[Dict[str, Any], Any], Any]

# group → (role or tool_name) → annotators
_ANNOTATORS: Dict[str, Dict[str, List[AnnotatorFn]]] = {}


def register_result_annotator(group: str, role: str, fn: AnnotatorFn) -> None:
    """Attach one annotator to ``role`` within ``group`` (additive;
    idempotent for the same function object)."""
    fns = _ANNOTATORS.setdefault(group, {}).setdefault(role, [])
    if fn not in fns:
        fns.append(fn)


def run_result_annotators(
    tool_name: str,
    args: Dict[str, Any],
    result: Any,
    scope: FlavorScope = EMPTY_SCOPE,
) -> Any:
    """Run the annotators ``scope`` puts in play for ``tool_name``, in
    registration order."""
    fns: Optional[List[AnnotatorFn]] = None
    for group, key in scoped_keys(tool_name, scope):
        fns = _ANNOTATORS.get(group, {}).get(key)
        if fns:
            break
    if not fns:
        return result
    for fn in fns:
        try:
            result = fn(args, result)
        except Exception as exc:  # noqa: BLE001 — fail-open by contract
            logger.warning(
                f"result annotator for {tool_name!r} raised ({exc}); skipping"
            )
    return result


__all__ = ["AnnotatorFn", "register_result_annotator", "run_result_annotators"]
