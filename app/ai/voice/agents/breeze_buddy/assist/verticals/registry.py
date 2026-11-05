"""The vertical registry — the engine's only door to a vertical."""

from __future__ import annotations

from typing import Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.commerce.vertical import (
    vertical as commerce,
)
from app.ai.voice.agents.breeze_buddy.assist.verticals.base import Vertical

VERTICALS: Tuple[Vertical, ...] = (commerce,)
DEFAULT: Vertical = commerce


def resolve(vertical_id: str) -> Vertical:
    for vertical in VERTICALS:
        if vertical.id == vertical_id:
            return vertical
    raise KeyError(f"unknown vertical: {vertical_id!r}")


def for_request(requested: Optional[str]) -> Vertical:
    """The vertical behind a request's ``vertical`` value; the default when absent."""
    if requested is None:
        return DEFAULT
    for vertical in VERTICALS:
        if vertical.request_vertical == requested:
            return vertical
    raise KeyError(f"unknown vertical: {requested!r}")


__all__ = ["DEFAULT", "VERTICALS", "for_request", "resolve"]
