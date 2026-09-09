"""Request and report shapes for ``POST /assist/probe``.

The report is what the console shows an operator before anything is built:
which platform we recognised, how sure we are, and the evidence — so a wrong
answer is arguable instead of mysterious.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    Classification,
    SiteProfile,
)

MAX_REPORTED_SIGNALS = 40


class ProbeRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(None, max_length=255)


class ProbeIdentity(BaseModel):
    platform: str
    canonical_host: str
    # The stable host the platform knows this site by, when it publishes one
    # and it differs from the domain the visitor typed.
    permanent_host: Optional[str] = None


class ProbeScriptHost(BaseModel):
    """Where a page's scripts come from, and how many from each."""

    host: str
    scripts: int


class ProbeSite(BaseModel):
    final_url: str
    status: int
    title: Optional[str] = None
    description: Optional[str] = None
    name: Optional[str] = None
    # True when the fetch was answered by an anti-bot interstitial: the
    # recognition below is then about the interstitial, not the site.
    challenge: bool = False
    # The page was longer than the read ceiling. What is reported is real,
    # but it is not all of it.
    truncated: bool = False
    size_bytes: int = 0
    fetched_at: Optional[datetime] = None
    # Busiest host first. The ratio of first-party to third-party scripts is
    # the earliest honest signal of how hard this site will be to mirror.
    script_hosts: List[ProbeScriptHost] = Field(default_factory=list)
    # Every type present, including in blocks too large to carry below.
    structured_data_types: List[str] = Field(default_factory=list)
    # The blocks themselves, capped by count and size: a page's own account of
    # its brand, logo and contacts, already parsed, so the research stage does
    # not have to fetch the page again to read it.
    structured_data: List[Dict[str, Any]] = Field(default_factory=list)


class ProbeSignal(BaseModel):
    kind: str
    pattern: str


class ProbeResponse(BaseModel):
    platform: str
    confidence: float
    scores: Dict[str, float] = Field(default_factory=dict)
    identity: ProbeIdentity
    site: ProbeSite
    matched_signals: List[ProbeSignal] = Field(default_factory=list)


def build_report(
    profile: SiteProfile,
    classification: Classification,
    summary: Dict[str, Any],
) -> ProbeResponse:
    return ProbeResponse(
        platform=classification.adapter_id,
        confidence=classification.confidence,
        scores=classification.scores,
        identity=ProbeIdentity(
            platform=classification.identity.platform,
            canonical_host=classification.identity.canonical_host,
            permanent_host=classification.identity.permanent_host,
        ),
        site=ProbeSite(**summary),
        matched_signals=[
            ProbeSignal(kind=signal.kind, pattern=signal.pattern)
            for signal in classification.matched[:MAX_REPORTED_SIGNALS]
        ],
    )


__all__ = [
    "MAX_REPORTED_SIGNALS",
    "ProbeIdentity",
    "ProbeScriptHost",
    "ProbeRequest",
    "ProbeResponse",
    "ProbeSignal",
    "ProbeSite",
    "build_report",
]
