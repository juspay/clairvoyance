"""The engine's shared door to the open web.

Not a stage — plumbing every stage that reads a merchant's site goes
through. ``fetch`` is the guarded GET (the service's SSRF boundary, and the
only place those rules live); ``html_facts`` turns one page into the handful
of facts we read off it. The probe uses both for a home page, and the
research stage uses the same two for a dozen more.
"""

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    BROWSER_USER_AGENT,
    FetchFailedError,
    FetchResult,
    UnsafeUrlError,
    fetch_page,
    normalize_probe_url,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.html_facts import PageFacts

__all__ = [
    "BROWSER_USER_AGENT",
    "FetchFailedError",
    "FetchResult",
    "PageFacts",
    "UnsafeUrlError",
    "fetch_page",
    "normalize_probe_url",
]
