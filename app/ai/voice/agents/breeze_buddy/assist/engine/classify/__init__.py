"""Stage 2 — decide which adapter handles this site, and say why.

``verdict`` scores every adapter over a probe's observations; ``signals``
holds what it means for a fingerprint to match one, which the adapters use
too. The evidence lives here rather than in an adapter so that recognising
a new platform stays a new adapter plus a registry entry.
"""

from app.ai.voice.agents.breeze_buddy.assist.engine.classify.signals import (
    signal_matches,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.classify.verdict import (
    classify_profile,
)

__all__ = ["classify_profile", "signal_matches"]
