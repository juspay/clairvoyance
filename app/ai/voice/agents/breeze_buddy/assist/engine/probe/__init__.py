"""Stage 1 — read a site's home page once and write down what it showed.

The probe has no opinion about what it is looking at: it records
observations and leaves the meaning to stage 2. Fetching and page parsing
come from ``engine.web``, which the later stages share.
"""

from app.ai.voice.agents.breeze_buddy.assist.engine.probe.profile import (
    is_challenge,
    probe_site,
    profile_from_fetch,
    profile_from_page,
    summarize,
)

__all__ = [
    "is_challenge",
    "probe_site",
    "profile_from_fetch",
    "profile_from_page",
    "summarize",
]
