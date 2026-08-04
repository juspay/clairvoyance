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
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from app.core.logger import logger

# (args, post-pipeline result) → annotated result (same object or enriched copy).
AnnotatorFn = Callable[[Dict[str, Any], Any], Any]

_ANNOTATORS: Dict[str, List[AnnotatorFn]] = {}


def register_result_annotator(tool_name: str, fn: AnnotatorFn) -> None:
    """Attach one annotator to ``tool_name`` (additive; idempotent for the
    same function object)."""
    fns = _ANNOTATORS.setdefault(tool_name, [])
    if fn not in fns:
        fns.append(fn)


def run_result_annotators(tool_name: str, args: Dict[str, Any], result: Any) -> Any:
    """Run every annotator for ``tool_name`` in registration order."""
    fns = _ANNOTATORS.get(tool_name)
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
