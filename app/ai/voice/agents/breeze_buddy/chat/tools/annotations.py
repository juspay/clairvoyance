"""Tool annotations — read_only / idempotent / destructive (Phase 2).

The safety metadata that lets the agent loop make execution decisions in
CODE instead of trusting the model:

- ``read_only`` — no side effects; safe to run in PARALLEL with other
  read-only calls from the same cycle (search + variant lookups fan out).
- ``idempotent`` — has side effects but carries idempotency protection
  (e.g. the injected idempotency hash on cart mutations); retried safely,
  but still SERIALIZED — mutation order is meaningful.
- ``destructive`` — side effects without idempotency protection; always
  serialized, and the natural tier for approval gating.

Resolution order (first hit wins):
  1. The template's ``configurations.tool_annotations`` override.
  2. The process-global registry (flavor modules register their tool
     surfaces at lazy-load time, e.g. commerce's UCP tools).
  3. ``destructive`` — the safe default: an unknown tool never
     accidentally runs in parallel.

Like the step-label registry, this module is flavor-blind: it holds the
mechanism; names come from flavor packages and templates.
"""

from __future__ import annotations

from typing import Any, Dict, Literal

ToolAnnotation = Literal["read_only", "idempotent", "destructive"]

_VALID: frozenset = frozenset(("read_only", "idempotent", "destructive"))

# Process-global registry — additive-only, populated by flavor modules at
# (lazy) import time. Per-template overrides never mutate this.
_ANNOTATIONS: Dict[str, ToolAnnotation] = {}


def register_tool_annotations(annotations: Dict[str, ToolAnnotation]) -> None:
    """Register default annotations for a set of tool names (flavor hook).

    Idempotent for identical re-registration; a conflicting re-register
    raises — two flavors disagreeing about a tool's safety class is a
    packaging bug, not a runtime preference.
    """
    for name, annotation in annotations.items():
        if annotation not in _VALID:
            raise ValueError(f"invalid tool annotation {annotation!r} for {name!r}")
        existing = _ANNOTATIONS.get(name)
        if existing is not None and existing != annotation:
            raise ValueError(
                f"tool {name!r} already annotated {existing!r}; refusing "
                f"{annotation!r}"
            )
        _ANNOTATIONS[name] = annotation


def resolve_tool_annotation(tool_name: str, template: Any = None) -> ToolAnnotation:
    """The effective annotation for ``tool_name`` under ``template``."""
    configurations = getattr(template, "configurations", None)
    overrides = getattr(configurations, "tool_annotations", None)
    if isinstance(overrides, dict):
        override = overrides.get(tool_name)
        if override in _VALID:
            # pyrefly sees `override` as plain str off the dict; the
            # membership check above IS the narrowing.
            return override  # type: ignore[return-value]
    return _ANNOTATIONS.get(tool_name, "destructive")


def is_read_only(tool_name: str, template: Any = None) -> bool:
    return resolve_tool_annotation(tool_name, template) == "read_only"


__all__ = [
    "ToolAnnotation",
    "register_tool_annotations",
    "resolve_tool_annotation",
    "is_read_only",
]
