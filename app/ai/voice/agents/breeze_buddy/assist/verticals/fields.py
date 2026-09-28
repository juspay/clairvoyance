"""The fields an assistant is built from, and how research findings fill them.

Research writes loose ``field: value`` notes. The merchant edits a form. This
turns the one into the other, by plain rules and no model call, so the same
notes always give the same form.

What an assistant needs to know depends on the business, so each vertical
declares its own ``FieldProfile``; this file only knows the shape.

Nothing is invented here: a field no note speaks to stays empty, and an empty
field is a question for the merchant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Optional, Tuple

# Field key → its values, in the order they were found. What a template keeps
# and what the merchant edits.
AssistFields = Dict[str, List[str]]

# Which editor a field is drawn with; a ``list`` field holds several lines.
FieldKind = Literal["line", "text", "list", "phone", "email"]

# Values kept for a field that holds several.
_MAX_VALUES_PER_FIELD = 12
# Longest value kept; research caps a note at the same length.
_MAX_VALUE_CHARS = 500
# A Markdown heading mark: one or more "#" and a space, at the start or after
# a space ("Sale on. ## Operating principles"). "#1" and "#MadeInIndia" are not.
_HEADING = re.compile(r"(?:^|(?<=\s))#+\s+")


@dataclass(frozen=True)
class FieldSpec:
    """One thing the form asks."""

    key: str
    label: str
    hint: str = ""
    kind: FieldKind = "line"
    # A real answer, shown as the placeholder.
    example: str = ""
    # Fields that repeat together share a group; ``index`` counts from 1.
    group: str = ""
    index: int = 0
    # Research may fill it (from the note of the same name). The others only
    # the merchant answers: the assistant's name, its greeting, its questions.
    from_research: bool = False

    @property
    def many(self) -> bool:
        """Holds several lines (a list of offers) rather than one value."""
        return self.kind == "list"


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

    sections: Tuple[FieldSection, ...] = field(default_factory=tuple)

    def specs(self) -> List[FieldSpec]:
        return [spec for section in self.sections for spec in section.fields]

    def spec(self, key: str) -> Optional[FieldSpec]:
        return next((spec for spec in self.specs() if spec.key == key), None)


def fields_from_notes(
    notes: Iterable[Tuple[str, str]], profile: FieldProfile
) -> AssistFields:
    """Research notes, as ``(note name, value)`` pairs, sorted into the form:
    a note fills the ``from_research`` field of the same name.

    A one-value field takes the first note that fills it, except a ``text``
    one, which joins its notes into one paragraph (research returns short
    pieces: "Polos", "Shirts"). A many-value field keeps up to
    ``_MAX_VALUES_PER_FIELD``, in order, without repeats (case, spaces and
    punctuation ignored). Each value goes through ``_clean_value``. Notes no
    field takes, and values with no letter or digit ("...", "-"), are dropped.
    """
    out: AssistFields = {}
    for name, raw in notes:
        spec = profile.spec(name)
        value = _clean_value(raw)
        if spec is None or not spec.from_research or not _same(value):
            continue
        values = out.setdefault(spec.key, [])
        if spec.kind == "text":
            _join(values, value)
            continue
        limit = _MAX_VALUES_PER_FIELD if spec.many else 1
        if len(values) < limit and _same(value) not in map(_same, values):
            values.append(value)
    return out


def _clean_value(raw: object) -> str:
    """One value as it may reach a prompt, from research or from the merchant.

    Template markers (``{{``, ``}}``) and every heading mark (``#`` then a
    space) removed, so a value can never open a template section, a prompt
    heading, or carry a heading line a prompt is split on ("## Operating
    principles"). "#1 in India" and "#MadeInIndia" keep their ``#``. Spaces
    collapsed; trimmed to ``_MAX_VALUE_CHARS``.
    """
    text = str(raw or "")
    # Again until none is left: removing "}}" from "{}}{" makes a new "{{".
    while "{{" in text or "}}" in text:
        text = text.replace("{{", "").replace("}}", "")
    return " ".join(_HEADING.sub("", text).split())[:_MAX_VALUE_CHARS]


def _same(value: str) -> str:
    """What two values compare on: "Crew necks" and "CrewNecks" are one."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _join(values: List[str], value: str) -> None:
    """Add the new parts of ``value`` ("Shirts, Caps" adds "Caps" after
    "Polos, Shirts") to the one paragraph in ``values``, whole or not at all."""
    parts = values[0].split(", ") if values else []
    seen = {_same(part) for part in parts}
    added = False
    # A part already there, or one with no letter or digit ("-"), adds nothing.
    for part in value.rstrip(" .;,").split(", "):
        key = _same(part)
        if key and key not in seen:
            seen.add(key)
            parts.append(part)
            added = True
    joined = ", ".join(parts)
    if added and len(joined) <= _MAX_VALUE_CHARS:
        values[:] = [joined]


__all__ = [
    "AssistFields",
    "FieldKind",
    "FieldProfile",
    "FieldSection",
    "FieldSpec",
    "fields_from_notes",
]
