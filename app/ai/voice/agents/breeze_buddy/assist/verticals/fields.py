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
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

# Field key → its values, in the order they were found. What a template keeps
# and what the merchant edits.
AssistFields = Dict[str, List[str]]

# Which editor a field is drawn with; a ``list`` field holds several lines.
FieldKind = Literal["line", "text", "list", "phone", "email"]

# Values kept for a field that holds several.
_MAX_VALUES_PER_FIELD = 12
# Longest value kept; research caps a note at the same length.
_MAX_VALUE_CHARS = 500
# A merchant's own edits may be longer: a hand-written section is often
# more than research would ever return, and must not be cut on a save.
_MAX_EDIT_VALUES = 50
_MAX_EDIT_CHARS = 4000
# A Markdown heading mark: "#"s and a space, anywhere ("Sale on. ## Operating
# principles", "x## ..."). A lone "#" right after a letter or digit is not one
# ("C# shop"), nor is "#1" or "#MadeInIndia" (no space after).
_HEADING = re.compile(r"(?<![A-Za-z0-9])#+\s+|#{2,}\s+")
# A question and its answer, written "Q: ... A: ...".
_FAQ_QUESTION = re.compile(r"Q:\s*(.+?)\s+A:")


@dataclass(frozen=True)
class FieldSpec:
    """One thing the form asks."""

    key: str
    label: str
    hint: str = ""
    kind: FieldKind = "line"
    # Research may fill it (from the note of the same name). The others only
    # the merchant answers: the assistant's name, its greeting, its first screen.
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

    A one-value field takes the first note that fills it; a ``text`` one the
    first that is a whole sentence (four words or more), the longest until
    then, never a join of menu words ("Jumpsuits", "Leggings"). A many-value
    field keeps up to ``_MAX_VALUES_PER_FIELD``, in order, without repeats
    (case, spaces and punctuation ignored), and one answer per question: the
    longer one ("14 days from delivery" over "Easy returns"). Each value goes through ``clean_value``. Notes no
    field takes, and values with no letter or digit ("...", "-"), are dropped.
    """
    out: AssistFields = {}
    for name, raw in notes:
        spec = profile.spec(name)
        value = clean_value(raw)
        if spec is None or not spec.from_research or not _same(value):
            continue
        values = out.setdefault(spec.key, [])
        if spec.kind == "text":
            if not values or (
                len(values[0].split()) < 4
                and len(value) > len(values[0])
                and _same(value) != _same(values[0])
            ):
                values[:] = [value]
            continue
        asked = _faq_question(value)
        same = next(
            (i for i, v in enumerate(values) if asked and _faq_question(v) == asked),
            None,
        )
        if same is not None:
            if len(value) > len(values[same]):
                values[same] = value
            continue
        limit = _MAX_VALUES_PER_FIELD if spec.many else 1
        if len(values) < limit and _same(value) not in map(_same, values):
            values.append(value)
    return out


def apply_edits(
    current: Mapping[str, Sequence[str]],
    edits: Mapping[str, Sequence[str]],
    profile: FieldProfile,
) -> AssistFields:
    """The fields after a merchant's edits.

    Only fields the form has can be edited; ``ValueError`` names any other.
    A field not sent keeps its value, so a console that does not know a field
    yet cannot blank it; a field sent empty is cleared. A ``text`` field keeps
    its line breaks and paragraphs (a two-line greeting); any other is made
    one line. Each line goes through ``clean_value``, the rule research
    findings go through, and repeats are dropped. A value too long or a list
    too long is refused, never cut.
    """
    out: AssistFields = {key: list(values) for key, values in current.items()}
    for key, values in edits.items():
        spec = profile.spec(key)
        if spec is None:
            raise ValueError(f"{key} cannot be edited")
        cleaned: List[str] = []
        for raw in values:
            # Refused before cleaning, which would cut it to the limit.
            if len(str(raw or "")) > _MAX_EDIT_CHARS:
                raise ValueError(f"{key} is longer than {_MAX_EDIT_CHARS} characters")
            lines = [
                clean_value(line, limit=_MAX_EDIT_CHARS)
                for line in str(raw or "").splitlines()
            ]
            if spec.kind == "text":
                value = "\n".join(lines).strip("\n")
            else:
                value = " ".join(line for line in lines if line)
            if len(value) > _MAX_EDIT_CHARS:
                raise ValueError(f"{key} is longer than {_MAX_EDIT_CHARS} characters")
            if value and _same(value) not in map(_same, cleaned):
                cleaned.append(value)
        limit = _MAX_EDIT_VALUES if spec.many else 1
        if len(cleaned) > limit:
            raise ValueError(f"{key} takes at most {limit}")
        out[key] = cleaned
    return out


def clean_value(raw: object, *, limit: int = _MAX_VALUE_CHARS) -> str:
    """One value as it may reach a prompt, from research or from the merchant.

    Every brace and every heading mark (``#``s then a space) removed, so a
    value can never open a template section (``{{...}}``), name a runtime
    placeholder (``{openai_api_key}`` would be filled with a secret), start a
    prompt heading, or carry the "## Operating principles" line a prompt is
    split on. "#1 in India", "#MadeInIndia" and "C# shop" keep their ``#``.
    Spaces collapsed; trimmed to ``limit`` characters.
    """
    # Control characters become spaces first: "##\x01 x" would hide its mark
    # here and turn back into a heading once a later step drops the \x01.
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch for ch in str(raw or "")[:limit]
    )
    text = text.replace("{", "").replace("}", "")
    return " ".join(_HEADING.sub("", text).split())


def _same(value: str) -> str:
    """What two values compare on: "Crew necks" and "CrewNecks" are one."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _faq_question(value: str) -> str:
    """The question of a "Q: ... A: ..." value, as compared; "" for any other."""
    asked = _FAQ_QUESTION.match(value)
    return _same(asked.group(1)) if asked else ""


__all__ = [
    "AssistFields",
    "FieldKind",
    "FieldProfile",
    "FieldSection",
    "FieldSpec",
    "apply_edits",
    "clean_value",
    "fields_from_notes",
]
