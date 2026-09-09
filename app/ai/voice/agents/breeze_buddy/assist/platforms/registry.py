"""The platform registry — the engine's only door to a platform.

Adding a platform is a package under ``platforms/`` plus one entry here.
Stages call ``resolve`` / ``classify``; they never import an adapter module.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import PlatformAdapter
from app.ai.voice.agents.breeze_buddy.assist.platforms.generic.adapter import (
    adapter as generic,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.adapter import (
    adapter as shopify,
)

# Order = classification priority among equal scores. ``generic`` is last and
# scores zero by construction, so it only wins when nothing else matches.
PLATFORMS: Tuple[PlatformAdapter, ...] = (shopify, generic)
CLASSIFY_THRESHOLD = 0.5


def resolve(adapter_id: str) -> PlatformAdapter:
    for adapter in PLATFORMS:
        if adapter.id == adapter_id:
            return adapter
    raise KeyError(f"unknown platform adapter: {adapter_id!r}")


def classify(signals: Sequence[Signal]) -> Tuple[PlatformAdapter, float]:
    """(adapter, confidence) for a probe's signals; ``generic`` below threshold."""
    best, best_score = generic, 0.0
    for adapter in PLATFORMS:
        score = adapter.classify(signals)
        if score > best_score:
            best, best_score = adapter, score
    if best_score < CLASSIFY_THRESHOLD:
        return generic, best_score
    return best, best_score


__all__ = ["CLASSIFY_THRESHOLD", "PLATFORMS", "classify", "resolve"]
