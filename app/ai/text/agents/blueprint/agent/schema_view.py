"""Compact, LLM-friendly view of the template schema.

The single-turn handler in :mod:`blueprint.agent.turn` doesn't want to
read the full :class:`TemplateSchemaGraph` — it has 111 leaf fields and
3 sub-schemas. Instead it sees a hand-curated, group-ordered view with
just the fields that are user-facing (askable groups), each annotated
with the bits the LLM actually needs to reason: type, recommendation,
alternatives, conditional skip rules, and the canonical dotted path it
must use when emitting ``draft_patch`` keys.

Built once at process start (cached) since the schema graph is static
for the life of the process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.ai.text.agents.blueprint.schema.graph import build_schema_graph
from app.ai.text.agents.blueprint.schema.groups import (
    ASKABLE_GROUPS,
    should_skip_group,
)
from app.ai.text.agents.blueprint.schema.models import (
    Coupling,
    FieldKind,
    FieldNode,
    TemplateSchemaGraph,
)


class AlternativeView(BaseModel):
    """One use-case override the LLM may pick instead of the recommendation."""

    value: Any
    when: str
    why: str


class FieldView(BaseModel):
    """A single field as the turn LLM sees it.

    ``path`` is the canonical dotted path the LLM must use as a
    ``draft_patch`` key — anything else won't merge into the draft.
    """

    path: str
    label: str
    type: str  # "string" | "bool" | "int" | "float" | "enum" | "list" | "other"
    enum: Optional[list[str]] = None
    required: bool
    recommended: Optional[Any] = None
    why_recommended: Optional[str] = None
    alternatives: list[AlternativeView] = Field(default_factory=list)
    rationale: Optional[str] = None


class CouplingView(BaseModel):
    """Plain-English statement of one structural rule."""

    name: str
    statement: str  # human-readable "if X then Y" form


class GroupView(BaseModel):
    """A configuration group in the order the conversation should walk."""

    name: str
    fields: list[FieldView]
    skip_when: Optional[str] = None
    """Plain-English condition under which the LLM should skip this group."""


class SchemaView(BaseModel):
    """Top-level compact view returned by :func:`build_schema_view`."""

    groups: list[GroupView]
    couplings: list[CouplingView]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

# Plain-English skip conditions, mirroring the structural rules in
# ``schema/groups.py``. Kept here because the LLM reads English, not
# Python predicates.
_SKIP_CONDITIONS: dict[str, str] = {
    "ivr": "skip when configurations.enable_inbound is false",
    "vad": (
        "skip when configurations.stt_configuration.turn_detection != 'smart_turn' "
        "(Silero VAD is only consumed in smart_turn mode)"
    ),
}


@lru_cache(maxsize=1)
def build_schema_view() -> SchemaView:
    """Build (and cache) the compact LLM-facing schema view."""
    graph = build_schema_graph()
    return _build_schema_view(graph)


def _build_schema_view(graph: TemplateSchemaGraph) -> SchemaView:
    groups = [_build_group(graph, name) for name in ASKABLE_GROUPS]
    groups = [g for g in groups if g.fields]  # drop groups with no askable fields
    couplings = [_build_coupling(c) for c in graph.couplings]
    return SchemaView(groups=groups, couplings=couplings)


def _build_group(graph: TemplateSchemaGraph, name: str) -> GroupView:
    fields = [
        _build_field(f)
        for f in graph.by_group(name)
        if not f.deprecated
        and f.kind != FieldKind.NESTED
        and f.kind != FieldKind.OPAQUE
    ]
    return GroupView(name=name, fields=fields, skip_when=_SKIP_CONDITIONS.get(name))


def _build_field(f: FieldNode) -> FieldView:
    return FieldView(
        path=f.path,
        label=_humanize_path(f.path),
        type=_field_type(f),
        enum=f.enum_values,
        required=f.required,
        recommended=f.recommendation.value if f.recommendation else None,
        why_recommended=f.recommendation.justification if f.recommendation else None,
        alternatives=[
            AlternativeView(value=alt.value, when=alt.when, why=alt.justification)
            for alt in f.recommendation_alternatives
        ],
        rationale=f.rationale,
    )


def _build_coupling(c: Coupling) -> CouplingView:
    triggers = " AND ".join(f"{p}={v!r}" for p, v in c.trigger.items())
    effects = []
    for e in c.effects:
        if e.kind.value == "required":
            effects.append(f"{e.path} must be set")
        elif e.kind.value == "equals":
            effects.append(f"{e.path} must equal {e.value!r}")
        elif e.kind.value == "forbidden":
            effects.append(f"{e.path} must be unset")
    statement = f"when {triggers}, then {', '.join(effects)}. {c.reason}".strip()
    return CouplingView(name=c.name, statement=statement)


# ---------------------------------------------------------------------------
# Runtime helpers (the turn handler will use these)
# ---------------------------------------------------------------------------


def remaining_groups(
    view: SchemaView,
    draft: dict[str, Any],
    completed: list[str],
    skipped: Optional[list[str]] = None,
) -> list[str]:
    """Groups left to walk through.

    Filters out three categories:

    * ``completed`` — LLM gathered a meaningful answer.
    * ``skipped`` — LLM determined the user has no requirement here.
    * structural auto-skips via :func:`should_skip_group` (e.g. ``ivr`` when
      inbound is off) — invisible to the user.
    """
    skipped_set = set(skipped or ())
    out: list[str] = []
    for g in view.groups:
        if g.name in completed or g.name in skipped_set:
            continue
        if should_skip_group(g.name, draft):
            continue
        out.append(g.name)
    return out


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _humanize_path(path: str) -> str:
    """``configurations.stt_configuration.provider`` → ``stt provider``."""
    short = path.split(".", 1)[1] if "." in path else path
    parts = []
    for seg in short.split("."):
        cleaned = seg.removesuffix("_configuration").removesuffix("_config")
        parts.append(cleaned.replace("_", " "))
    return " ".join(parts).strip()


def _field_type(f: FieldNode) -> str:
    if f.kind == FieldKind.ENUM:
        return "enum"
    if f.kind == FieldKind.LIST:
        return "list"
    if f.kind == FieldKind.DICT:
        return "dict"
    py = (f.py_type or "").lower()
    if "bool" in py:
        return "bool"
    if "int" in py and "float" not in py:
        return "int"
    if "float" in py:
        return "float"
    if "str" in py:
        return "string"
    return "other"


__all__ = [
    "AlternativeView",
    "CouplingView",
    "FieldView",
    "GroupView",
    "SchemaView",
    "build_schema_view",
    "remaining_groups",
]
