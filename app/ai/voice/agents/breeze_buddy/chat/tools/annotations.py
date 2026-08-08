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
  1. The template's ``configurations.tool_execution.annotations`` override.
  2. The registry of a flavor group this template enabled (flavor modules
     register their tool surfaces at lazy-load time, e.g. commerce's UCP
     tools), matched by ROLE so a template that rebound the tool keeps its
     safety class.
  3. ``destructive`` — the safe default: an unknown tool never
     accidentally runs in parallel.

Like the step-label registry, this module is flavor-blind: it holds the
mechanism; names come from flavor packages and templates. The group gate
(see ``chat/flavors.py``) is what stops a commerce annotation reclassifying
a same-named tool on a template that never enabled commerce — it decides
parallel fan-out and mutation serialization, so a wrong ``read_only`` runs
somebody else's mutations concurrently.
"""

from __future__ import annotations

from typing import Any, Dict, Literal

from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    EMPTY_SCOPE,
    FlavorScope,
    scoped_keys,
)

ToolAnnotation = Literal["read_only", "idempotent", "destructive"]

_VALID: frozenset = frozenset(("read_only", "idempotent", "destructive"))

# group → (role or tool_name) → annotation. Additive-only, populated by
# flavor modules at (lazy) import time. Per-template overrides never
# mutate this.
_ANNOTATIONS: Dict[str, Dict[str, ToolAnnotation]] = {}


def register_tool_annotations(
    group: str, annotations: Dict[str, ToolAnnotation]
) -> None:
    """Register default annotations for a group's tool roles (flavor hook).

    Idempotent for identical re-registration; a conflicting re-register
    within the same group raises — two flavors disagreeing about a tool's
    safety class is a packaging bug, not a runtime preference. Different
    groups may of course annotate the same name differently; that is the
    whole point of scoping them.
    """
    registered = _ANNOTATIONS.setdefault(group, {})
    for name, annotation in annotations.items():
        if annotation not in _VALID:
            raise ValueError(f"invalid tool annotation {annotation!r} for {name!r}")
        existing = registered.get(name)
        if existing is not None and existing != annotation:
            raise ValueError(
                f"tool {name!r} already annotated {existing!r} in group "
                f"{group!r}; refusing {annotation!r}"
            )
        registered[name] = annotation


def resolve_tool_annotation(
    tool_name: str, template: Any = None, scope: FlavorScope = EMPTY_SCOPE
) -> ToolAnnotation:
    """The effective annotation for ``tool_name`` under ``template``."""
    configurations = getattr(template, "configurations", None)
    tool_execution = getattr(configurations, "tool_execution", None)
    overrides = getattr(tool_execution, "annotations", None)
    if isinstance(overrides, dict):
        override = overrides.get(tool_name)
        if override in _VALID:
            # pyrefly sees `override` as plain str off the dict; the
            # membership check above IS the narrowing.
            return override  # type: ignore[return-value]
    for group, key in scoped_keys(tool_name, scope):
        annotation = _ANNOTATIONS.get(group, {}).get(key)
        if annotation is not None:
            return annotation
    return "destructive"


def is_read_only(
    tool_name: str, template: Any = None, scope: FlavorScope = EMPTY_SCOPE
) -> bool:
    return resolve_tool_annotation(tool_name, template, scope) == "read_only"


__all__ = [
    "ToolAnnotation",
    "register_tool_annotations",
    "resolve_tool_annotation",
    "is_read_only",
]
