"""Schema-graph data structures.

These are Blueprint's own representation of the Breeze Buddy template
surface — built at runtime by introspecting ``TemplateModel`` + children.
Consumers (planner, specialists, conversation layer) read from this graph
to decide what to ask, what's coupled, and what's still unfilled.
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class FieldKind(str, Enum):
    """How Blueprint should treat a field."""

    PRIMITIVE = "primitive"  # str, int, float, bool
    ENUM = "enum"  # Enum subclass or Literal[...]
    LIST = "list"  # List[T]
    DICT = "dict"  # Dict[K, V] where V is typed
    NESTED = "nested"  # nested BaseModel (recurse into it)
    UNION = "union"  # discriminated or plain union
    OPAQUE = "opaque"  # Dict[str, Any] / Any — treat as black box


class Recommendation(BaseModel):
    """Suggested default for a field plus the justification we show the user."""

    value: Any
    justification: str


class RecommendationAlternative(BaseModel):
    """Use-case-specific override for a field's default recommendation.

    The planner can pick this instead of the primary ``Recommendation``
    when the conversation context matches ``when``.
    """

    value: Any
    when: str
    justification: str


class FieldNode(BaseModel):
    """Single introspected field in the template surface."""

    path: str
    """Dotted path from the template root (e.g. ``configurations.stt_configuration.provider``)."""

    kind: FieldKind
    py_type: str
    """``repr(annotation)`` — kept for debugging / LLM context."""

    required: bool
    default: Any = None

    enum_values: Optional[list[str]] = None
    """Set when ``kind == ENUM``."""

    nested_model: Optional[str] = None
    """Class name of the nested model — for ``NESTED`` or for ``LIST`` item type."""

    item_kind: Optional[FieldKind] = None
    """For ``LIST`` / ``DICT`` fields: the kind of the value type."""

    description: Optional[str] = None
    deprecated: bool = False
    group: str
    """See ``schema.groups`` — UI-level cluster (stt, tts, vad, flow, ...)."""

    # --- Enrichment (populated by ``schema/enrich.py`` from the YAML file) ---
    rationale: Optional[str] = None
    """Why this field exists, in user-facing English. Overrides the terser
    ``description`` when present.
    """

    recommendation: Optional[Recommendation] = None
    """Hand-curated default + justification for the "this might pair well
    with…" conversational flow (Phase 3b).
    """

    recommendation_alternatives: list[RecommendationAlternative] = []
    """Use-case-specific overrides the planner can pick instead of
    :attr:`recommendation` when conversation context warrants it
    (e.g. user mentions Hindi → switch STT provider from default).
    """

    example_phrasings: list[str] = []
    """Short phrasings the planner can borrow when asking about this field."""


class ConstraintKind(str, Enum):
    REQUIRED = "required"  # target must be set (not None / not default)
    EQUALS = "equals"  # target must equal a specific value
    FORBIDDEN = "forbidden"  # target must be None / unset


class CouplingEffect(BaseModel):
    """What happens to the target field when the coupling's trigger fires."""

    path: str
    kind: ConstraintKind
    value: Any = None  # populated only for EQUALS
    reason: Optional[str] = None


class Coupling(BaseModel):
    """Declarative field-to-field dependency.

    ``trigger`` is a conjunction: all ``path == value`` entries must match
    the current draft for the coupling to fire. When it fires, every
    ``effect`` constrains the draft.
    """

    name: str
    trigger: dict[str, Any]
    effects: list[CouplingEffect]
    reason: str

    def fires(self, draft: dict[str, Any]) -> bool:
        """True when all trigger conditions are satisfied by ``draft``.

        Path dereferencing is dotted — ``"a.b.c"`` reads ``draft["a"]["b"]["c"]``
        and returns ``None`` if any intermediate key is missing.
        """
        for path, expected in self.trigger.items():
            if _resolve_path(draft, path) != expected:
                return False
        return True


class SubSchema(BaseModel):
    """Introspection result for a nested model that is not reachable from
    the root (e.g. ``FlowNodeModel`` — only referenced via
    ``flow: Dict[str, Any]`` which we mark opaque). Specialists can look
    these up when they need to author nodes / global functions.
    """

    name: str
    description: Optional[str] = None
    fields: list[FieldNode]


class TemplateSchemaGraph(BaseModel):
    """Indexed view of the template surface."""

    root: str
    """Name of the root model (typically ``TemplateModel``)."""

    fields: list[FieldNode]
    sub_schemas: dict[str, SubSchema] = Field(default_factory=dict)
    couplings: list[Coupling] = Field(default_factory=list)

    def by_path(self, path: str) -> Optional[FieldNode]:
        for f in self.fields:
            if f.path == path:
                return f
        return None

    def by_group(self, group: str) -> list[FieldNode]:
        return [f for f in self.fields if f.group == group]

    def groups(self) -> list[str]:
        """Distinct groups in declaration order."""
        seen: list[str] = []
        for f in self.fields:
            if f.group not in seen:
                seen.append(f.group)
        return seen

    def active_couplings(self, draft: dict[str, Any]) -> list[Coupling]:
        return [c for c in self.couplings if c.fires(draft)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    """Walk a dotted path, returning ``None`` on any missing segment."""
    cur: Any = data
    for segment in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(segment)
        if cur is None:
            return None
    return cur


__all__ = [
    "ConstraintKind",
    "Coupling",
    "CouplingEffect",
    "FieldKind",
    "FieldNode",
    "Recommendation",
    "RecommendationAlternative",
    "SubSchema",
    "TemplateSchemaGraph",
]
