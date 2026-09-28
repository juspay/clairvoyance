"""The fields an assistant is built from, and how research findings fill them.

Research writes loose ``field: value`` notes. The merchant edits a form. This
turns the one into the other, by plain rules and no model call, so the same
notes always give the same form.

**The field list is not the engine's.** What an assistant needs to know
depends on the business, so each vertical declares its own ``FieldProfile``;
the engine only knows the shape.

Nothing is invented here: a field no note speaks to stays empty, and an empty
field is a question for the merchant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# Field key → its values, in the order they were found. What a template keeps
# and what the merchant edits.
AssistFields = Dict[str, List[str]]

# Values kept for a field that holds several.
MAX_VALUES_PER_FIELD = 12
# Longest value kept; research caps a note at the same length.
MAX_VALUE_CHARS = 500


@dataclass(frozen=True)
class FieldSpec:
    """One thing the form asks."""

    key: str
    label: str
    hint: str = ""
    # Holds several lines (a list of offers) rather than one value.
    many: bool = False
    # Which editor to draw: line, text, list, phone, email.
    kind: str = ""
    # A real answer, shown as the placeholder.
    example: str = ""
    # Fields that repeat together share a group; ``index`` counts from 1.
    group: str = ""
    index: int = 0
    # Reaches the prompt but is not put to the merchant as a question.
    hidden: bool = False
    # The research note names that fill this field; empty means research
    # never fills it (the merchant does).
    from_notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldSection:
    """Fields a merchant reads and edits together."""

    key: str
    title: str
    brief: str = ""
    fields: Tuple[FieldSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FieldProfile:
    """A vertical's whole form."""

    name: str
    sections: Tuple[FieldSection, ...] = field(default_factory=tuple)

    def specs(self) -> List[FieldSpec]:
        return [spec for section in self.sections for spec in section.fields]

    def spec(self, key: str) -> Optional[FieldSpec]:
        return next((spec for spec in self.specs() if spec.key == key), None)


def fields_from_notes(
    notes: Iterable[Tuple[str, str]], profile: FieldProfile
) -> AssistFields:
    """Research notes, as ``(note name, value)`` pairs, sorted into the form.

    A one-value field takes the first note that fills it, except a ``text``
    one, which joins its notes into one paragraph (research returns short
    pieces: "Polos", "Shirts"). A many-value field keeps up to
    ``MAX_VALUES_PER_FIELD``, in order, without repeats. Values are trimmed
    to ``MAX_VALUE_CHARS``. Notes no field takes are dropped.
    """
    targets: Dict[str, FieldSpec] = {}
    for spec in profile.specs():
        for name in spec.from_notes:
            targets.setdefault(name, spec)

    out: AssistFields = {}
    for name, raw in notes:
        spec = targets.get(name)
        value = " ".join(str(raw or "").split())[:MAX_VALUE_CHARS]
        if spec is None or not value:
            continue
        values = out.setdefault(spec.key, [])
        if spec.kind == "text" and not spec.many:
            _join(values, value)
            continue
        limit = MAX_VALUES_PER_FIELD if spec.many else 1
        if len(values) < limit and value not in values:
            values.append(value)
    return out


def _join(values: List[str], value: str) -> None:
    """Add ``value`` to the one paragraph in ``values``, whole or not at all."""
    piece = value.rstrip(" .;,")
    if not values:
        values.append(piece)
        return
    parts = [part.lower() for part in values[0].split(", ")]
    joined = f"{values[0]}, {piece}"
    if piece and piece.lower() not in parts and len(joined) <= MAX_VALUE_CHARS:
        values[0] = joined


__all__ = [
    "AssistFields",
    "FieldProfile",
    "FieldSection",
    "FieldSpec",
    "MAX_VALUES_PER_FIELD",
    "MAX_VALUE_CHARS",
    "fields_from_notes",
]
