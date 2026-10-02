"""The shapes the console's edit page speaks: the form with its values, and an
edit. Taken from #1209 (schemas/breeze_buddy/assist/fields.py)."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field


class FieldOut(BaseModel):
    """One editable field: what it is called, what it is for, what it holds."""

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
    values: List[str] = Field(default_factory=list)


class FieldSectionOut(BaseModel):
    key: str
    title: str
    # One line on what filling this section in changes.
    brief: str = ""
    fields: List[FieldOut] = Field(default_factory=list)


class AssistFieldsResponse(BaseModel):
    """``GET /assist/agents/{template_id}/fields``.

    ``editable`` is false for an assistant not made from fields (one set up
    before them): the console shows its prompt instead, and ``sections`` is
    empty.
    """

    template_id: str
    editable: bool
    sections: List[FieldSectionOut] = Field(default_factory=list)


class AssistFieldsUpdate(BaseModel):
    """``PUT /assist/agents/{template_id}/fields``: the fields that changed.

    A field not sent keeps its value; a field sent with no values is cleared.
    """

    fields: Dict[str, List[str]] = Field(default_factory=dict, max_length=64)


__all__ = [
    "AssistFieldsResponse",
    "AssistFieldsUpdate",
    "FieldOut",
    "FieldSectionOut",
]
