"""Request and reply of ``/assist/template/{template_id}/fields``: the form with
its values, and an edit. Taken from #1209 (schemas/breeze_buddy/assist/fields.py)."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field


class TemplateField(BaseModel):
    """One editable field: what it is called, what it is for, what it holds."""

    key: str
    label: str
    hint: str = ""
    # Which editor to draw: line, text, list, phone, email.
    kind: str = ""
    values: List[str] = Field(default_factory=list)


class TemplateFieldsSection(BaseModel):
    key: str
    title: str
    # One line on what filling this section in changes.
    brief: str = ""
    fields: List[TemplateField] = Field(default_factory=list)


class TemplateFieldsResponse(BaseModel):
    """``GET /assist/template/{template_id}/fields``.

    ``sections`` is empty for an assistant whose prompt has no brand block
    (not an Assist assistant): the console shows its prompt instead.
    """

    sections: List[TemplateFieldsSection] = Field(default_factory=list)


class TemplateFieldsUpdateRequest(BaseModel):
    """``PUT /assist/template/{template_id}/fields``: the fields that changed.

    A field not sent keeps its value; a field sent with no values is cleared.
    """

    fields: Dict[str, List[str]]


__all__ = [
    "TemplateField",
    "TemplateFieldsResponse",
    "TemplateFieldsSection",
    "TemplateFieldsUpdateRequest",
]
