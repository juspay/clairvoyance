"""Stage 1 — read a site's home page once and write down what it showed.

The probe is the only stage that has no opinion about what it is looking at.
It fetches, parses, and records observations as ``Signal`` values; deciding
what they mean is stage 2's job, and knowing which of them matter belongs to
the adapters, which declare the names and markers they care about through the
registry (``probe_literals`` / ``probe_markers``).

Splitting the fetch from the reading is deliberate: :func:`profile_from_page`
is pure, so a captured page can be replayed in a test without a socket.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal, SiteProfile
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    FetchResult,
    fetch_page,
    normalize_probe_url,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.html_facts import PageFacts
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry

# A body that is an interstitial rather than the site. Recording this as a
# fact matters: a challenge page has none of the site's own markers, so a
# classifier that did not know would confidently answer "unrecognised" and
# the operator would be told their site is something it is not.
_CHALLENGE_STATUSES = frozenset({401, 403, 429, 503})
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "just a moment...",
    "attention required!",
    "enable javascript and cookies to continue",
    "px-captcha",
    "/_incapsula_resource",
    "are you a human",
)
_CHALLENGE_HEADERS = ("cf-mitigated", "x-datadome", "x-incapsula-rid")
# Header values are evidence, not payload: enough to match a fingerprint,
# short enough that a 4 KB header cannot bloat a stored artefact.
_SIGNAL_VALUE_CAP = 120
# The structured-data payload is a convenience for the next stage, not an
# archive: enough blocks to carry a page's own description of itself, each
# small enough that one page cannot bloat a report.
_MAX_REPORTED_STRUCTURED_BLOCKS = 10
_MAX_REPORTED_STRUCTURED_CHARS = 4_000


async def probe_site(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SiteProfile:
    """Fetch ``url`` under the engine's fetch guard and write down what came back."""
    normalized = normalize_probe_url(url)
    result = await fetch_page(
        normalized, timeout_seconds=timeout_seconds, max_bytes=max_bytes
    )
    return profile_from_fetch(result)


def profile_from_fetch(result: FetchResult) -> SiteProfile:
    return profile_from_page(
        url=result.url,
        final_url=result.final_url,
        status=result.status,
        headers=result.headers,
        cookie_names=result.cookie_names,
        body=result.body,
        size_bytes=result.size_bytes,
        truncated=result.truncated,
    )


def profile_from_page(
    *,
    url: str,
    final_url: str,
    status: int,
    headers: Mapping[str, str],
    cookie_names: Sequence[str],
    body: str,
    size_bytes: Optional[int] = None,
    truncated: bool = False,
    fetched_at: Optional[datetime] = None,
) -> SiteProfile:
    """One fetched page → the observations stage 2 reasons over. Pure."""
    facts = PageFacts(final_url or url, body)
    lowered_headers = {name.lower(): value for name, value in headers.items()}
    script_hosts = facts.script_hosts()
    literals = facts.literals(registry.probe_literals())
    markers = facts.markers(registry.probe_markers())

    signals: List[Signal] = []
    signals += [Signal(kind="script_src", pattern=host) for host in script_hosts]
    signals += [Signal(kind="cookie_key", pattern=name) for name in cookie_names]
    signals += [
        Signal(kind="header", pattern=_header_signal(name, value))
        for name, value in lowered_headers.items()
    ]
    signals += [Signal(kind="meta", pattern=key) for key in facts.meta]
    signals += [Signal(kind="js_literal", pattern=name) for name in literals]
    signals += [Signal(kind="js_global", pattern=marker) for marker in markers]

    return SiteProfile(
        url=url,
        final_url=final_url or url,
        status=status,
        title=facts.title,
        size_bytes=size_bytes if size_bytes is not None else len(body.encode("utf-8")),
        truncated=truncated,
        headers=dict(lowered_headers),
        cookies=list(cookie_names),
        meta=dict(facts.meta),
        link_rels=list(facts.link_rels),
        json_ld=list(facts.json_ld),
        script_hosts=script_hosts,
        inline_literals=literals,
        signals=signals,
        challenge=is_challenge(status, lowered_headers, body),
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )


def _header_signal(name: str, value: str) -> str:
    """``name: value``, printable and short.

    Header values are written by the site being looked at and end up in a
    report and, later, in stored artefacts. Control characters have no place
    in a fingerprint and would only ever be there to confuse something
    downstream, so they are dropped at the point of capture.
    """
    cleaned = "".join(character for character in value if character.isprintable())
    return f"{name}: {cleaned}".strip()[:_SIGNAL_VALUE_CAP]


def is_challenge(status: int, headers: Mapping[str, str], body: str) -> bool:
    """Is this an anti-bot interstitial rather than the site itself?

    A marker alone is enough — a challenge served with 200 is common — but a
    blocking status alone is not: a real page can answer 403 for other reasons.

    The last line deliberately also catches a short "Access Denied" page with
    no recognisable vendor marker. It is not a challenge in the strict sense,
    but it is the same thing for our purpose: whatever came back, it was not
    the site, so nothing downstream should treat it as evidence about the site.
    """
    if any(header in headers for header in _CHALLENGE_HEADERS):
        return True
    head = body[:20_000].lower()
    if any(marker in head for marker in _CHALLENGE_MARKERS):
        return True
    return status in _CHALLENGE_STATUSES and len(body) < 4_000


def summarize(profile: SiteProfile) -> Dict[str, Any]:
    """The operator-facing half of a probe report."""
    return {
        "final_url": profile.final_url,
        "status": profile.status,
        "title": profile.title or profile.meta.get("og:title"),
        "description": profile.meta.get("description")
        or profile.meta.get("og:description"),
        "name": profile.meta.get("og:site_name") or profile.title,
        "challenge": profile.challenge,
        "truncated": profile.truncated,
        "size_bytes": profile.size_bytes,
        "fetched_at": profile.fetched_at,
        # Busiest first: the shape of a page's script topology says more about
        # how hard it will be to mirror than an alphabetical list of names.
        "script_hosts": [
            {"host": host, "scripts": count}
            for host, count in sorted(
                profile.script_hosts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        # Complete, and cheap: derived from every block, including any the
        # payload below had to leave out.
        "structured_data_types": sorted(
            {
                str(block.get("@type"))
                for block in profile.json_ld
                if isinstance(block, dict) and block.get("@type")
            }
        ),
        "structured_data": _reportable_structured_data(profile.json_ld),
    }


def _reportable_structured_data(blocks: Sequence[Any]) -> List[Dict[str, Any]]:
    """The structured-data blocks themselves, bounded.

    A page's own description of itself — brand name, logo, contact details —
    already parsed here. Carrying it in the report saves the research stage a
    second fetch of a page it has already been given. Third-party content, so
    it is capped by count and by size rather than trusted to be small.
    """
    reportable: List[Dict[str, Any]] = []
    for block in blocks:
        if len(reportable) >= _MAX_REPORTED_STRUCTURED_BLOCKS:
            break
        if not isinstance(block, dict):
            continue
        try:
            encoded = json.dumps(block)
        except (TypeError, ValueError):
            continue
        if len(encoded) <= _MAX_REPORTED_STRUCTURED_CHARS:
            reportable.append(block)
    return reportable


__all__ = [
    "is_challenge",
    "probe_site",
    "profile_from_fetch",
    "profile_from_page",
    "summarize",
]
