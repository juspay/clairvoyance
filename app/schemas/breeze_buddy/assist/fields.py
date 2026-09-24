"""The shapes the studio speaks: a section list with values, and an update."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field


class FieldSpecOut(BaseModel):
    """One editable field: what it is called, what it is for, what it holds."""

    key: str
    label: str
    hint: str = ""
    # A field that holds several lines — a list of offers, a set of replies.
    many: bool = False
    # Which editor to draw: line, text, list, phone, email. Blank leaves the
    # choice to the console. The field list is the vertical's, and so is the
    # furniture it wants — a console that guessed would have to be taught
    # every new vertical.
    kind: str = ""
    # A real answer, shown as the placeholder.
    example: str = ""
    # For a list: the singular noun on its add button.
    item: str = ""
    # Fields that repeat together: same group name, index from 1. Instance 1
    # always shows; later ones show when filled or when the merchant adds one.
    group: str = ""
    index: int = 0
    # Shown, never written — a fact about where the agent lives.
    readonly: bool = False
    values: List[str] = Field(default_factory=list)


class FieldSectionOut(BaseModel):
    key: str
    title: str
    # One line on what filling this section in changes.
    brief: str = ""
    fields: List[FieldSpecOut] = Field(default_factory=list)


class AgentFieldsResponse(BaseModel):
    template_id: str
    platform: str = ""
    # Which field list this is. The console renders whatever arrives, so a new
    # vertical needs no console change.
    profile: str = ""
    # False when this agent has no studio — the console shows the prompt.
    editable: bool = False
    sections: List[FieldSectionOut] = Field(default_factory=list)


class AgentFieldsUpdate(BaseModel):
    """Edited values, keyed as the sections named them.

    Only fields present are written. Anything the studio does not send keeps
    whatever the live prompt already had, so a console that has not caught up
    with a new field cannot silently blank it.
    """

    fields: Dict[str, List[str]] = Field(default_factory=dict)


__all__ = [
    "AgentFieldsResponse",
    "AgentFieldsUpdate",
    "FieldSectionOut",
    "FieldSpecOut",
]
