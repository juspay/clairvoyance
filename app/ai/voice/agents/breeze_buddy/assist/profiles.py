"""Which sections an assistant is built from, by name.

The one place that knows both the engine's neutral profile and every
vertical's. It sits above both on purpose: the engine must not import a
vertical, and a vertical should not have to know about its siblings. An
adapter names a profile; this resolves it.
"""

from __future__ import annotations

from typing import Dict

from app.ai.voice.agents.breeze_buddy.assist.commerce.slots import (
    PROFILE as STORE_PROFILE,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.slots import (
    GENERIC_PROFILE,
    SlotProfile,
)

PROFILES: Dict[str, SlotProfile] = {
    GENERIC_PROFILE.name: GENERIC_PROFILE,
    STORE_PROFILE.name: STORE_PROFILE,
}


def resolve(name: str) -> SlotProfile:
    """The named profile, or the neutral one. Never raises: an unknown name
    should cost a merchant a less specific assistant, not a failed onboarding."""
    return PROFILES.get(name, GENERIC_PROFILE)


__all__ = ["PROFILES", "resolve"]
