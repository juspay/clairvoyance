"""Stage 2 — decide which adapter handles this site, and say why.

The engine holds the mechanism (score every adapter, take the best, fall back
below the threshold) and none of the evidence: each adapter scores the probe's
signals itself, so recognising a new platform is a new adapter and one registry
entry, never a branch here.

Falling back is a real answer, not a failure. An unrecognised site is handled
by the generic adapter, which is the path that must work everywhere.
"""

from __future__ import annotations

from typing import List

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    Classification,
    Signal,
    SiteProfile,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry


def classify_profile(profile: SiteProfile) -> Classification:
    """Which adapter owns this site, how sure we are, and what convinced us.

    A page that came back as an anti-bot interstitial carries none of the
    site's own markers, so expect the generic fallback with a low score; the
    profile's ``challenge`` flag is what tells the difference between "this
    site is unrecognised" and "we never saw this site".
    """
    adapter, confidence = registry.classify(profile.signals)
    return Classification(
        adapter_id=adapter.id,
        confidence=_reported(confidence),
        scores={
            name: _reported(score)
            for name, score in registry.score_all(profile.signals).items()
        },
        identity=adapter.identity(profile),
        matched=_matching_signals(adapter, profile.signals),
    )


def _reported(score: float) -> float:
    """A score as a confidence: 0 to 1.

    An adapter is free to keep adding weight past its own certainty — a page
    carrying every marker it knows scores well above the ceiling — and ranking
    uses that raw number. What is reported should read as a confidence, so it
    is capped here rather than in each adapter's table.
    """
    return round(min(score, 1.0), 4)


def _matching_signals(adapter, signals: List[Signal]) -> List[Signal]:
    """The signals that carried weight for the winner.

    Scored one at a time through the adapter's own scorer rather than by
    re-reading its table, which would mean knowing what its table looks like.
    """
    return [signal for signal in signals if adapter.classify([signal]) > 0.0]


__all__ = ["classify_profile"]
