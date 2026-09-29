"""Events of ``POST /assist/onboarding/research/stream``: a ``note`` per fact,
then one ``done`` or ``error``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AssistResearchNote(BaseModel):
    """One fact, and the page it was read on (a ``note`` event)."""

    field: str
    value: str
    source_url: str


class AssistResearchCompletion(BaseModel):
    """How the run went (the ``done`` event); its facts came as ``note`` events."""

    success: Literal[True] = True
    # "completed" when every chosen page was read; "timed_out" when the time
    # budget ended the run and the notes are what was found by then.
    status: Literal["completed", "timed_out"] = "completed"


class AssistResearchError(BaseModel):
    """Why the run ended without a result (the ``error`` event)."""

    success: Literal[False] = False
    message: str
    retryable: bool = False


__all__ = [
    "AssistResearchCompletion",
    "AssistResearchError",
    "AssistResearchNote",
]
