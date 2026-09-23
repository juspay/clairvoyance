"""Brand look from a rendered page, via Firecrawl's ``branding`` format.

Why a vendor at all: the colours a site *uses* are not the colours a site *is*.
Measured on live stores (2026-09-10), counting hex values in a page's CSS
returns the wrong answer — a third-party review widget's yellow on one store, a
retired accent on another, and on a third the real brand colour appeared in the
stylesheet exactly once. Reading the styles the browser actually computed for
the header and the primary button is a different question, and it is the one
worth asking.

Verified against ground truth on the same day: this returned ``#C22126`` for a
store whose platform brand block says ``#C22126``, and ``#B69D6C`` for one that
says ``#b69d6c``. It also correctly demoted to *accent* the exact colour that
frequency analysis had promoted to primary.

It is not infallible — on a well-known travel site it returned a brand colour
the company retired years ago — so the caller treats this as the best of
several sources rather than the answer, and the merchant confirms.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, cast

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandFont,
    BrandLook,
    ColorRole,
    FontRole,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.logger import logger

ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
SOURCE = "firecrawl:branding"
# A rendered scrape is 15-40 s. This belongs in an async stage with a budget,
# never on a request a person is waiting behind.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Their field name → the role it plays for us. Anything unlisted is ignored
# rather than guessed at.
_COLOR_ROLES: Mapping[str, ColorRole] = {
    "primary": "primary",
    "secondary": "secondary",
    "accent": "accent",
    "background": "background",
    "textPrimary": "text",
    "link": "link",
}
_FONT_ROLES: Mapping[str, FontRole] = {
    "heading": "heading",
    "body": "body",
    "mono": "mono",
}
_HEX_LENGTHS = (4, 7)


class BrandLookUnavailable(RuntimeError):
    """The provider could not answer. Never fatal — the caller has fallbacks."""


def configured() -> bool:
    return bool(FIRECRAWL_API_KEY)


async def brand_look(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> BrandLook:
    """Render ``url`` and read the brand tokens the browser computed."""
    headers = {"Content-Type": "application/json"}
    if FIRECRAWL_API_KEY:
        headers["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"
    else:
        # Unauthenticated calls were observed to work. That is undocumented and
        # will close; run without a key only in development.
        logger.warning("assist brand look: no FIRECRAWL_API_KEY configured")

    payload = {"url": url, "formats": ["branding"]}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                ENDPOINT, headers=headers, data=json.dumps(payload)
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise BrandLookUnavailable(
                        f"provider returned {response.status}: {body[:200]}"
                    )
    except aiohttp.ClientError as exc:
        raise BrandLookUnavailable(f"provider unreachable: {exc}") from exc
    except TimeoutError as exc:
        raise BrandLookUnavailable("provider timed out") from exc

    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise BrandLookUnavailable("provider returned invalid JSON") from exc

    branding = (parsed.get("data") or {}).get("branding")
    if not isinstance(branding, dict):
        raise BrandLookUnavailable("provider returned no branding block")
    return parse_branding(branding)


def parse_branding(branding: Dict[str, Any]) -> BrandLook:
    """Provider payload → ``BrandLook``. Pure, so a captured payload replays."""
    colors: List[BrandColor] = []
    raw_colors = branding.get("colors")
    if isinstance(raw_colors, Mapping):
        for field, role in _COLOR_ROLES.items():
            value = _hex(raw_colors.get(field))
            if value:
                colors.append(
                    BrandColor(role=role, hex=value, source=SOURCE, confidence=0.8)
                )

    fonts: List[BrandFont] = []
    for entry in branding.get("fonts") or []:
        if not isinstance(entry, Mapping):
            continue
        family = str(entry.get("family") or "").strip()
        if family:
            fonts.append(
                BrandFont(
                    family=family[:120],
                    role=_FONT_ROLES.get(
                        str(entry.get("role")), cast(FontRole, "unknown")
                    ),
                    source=SOURCE,
                )
            )

    scheme = branding.get("colorScheme")
    images = branding.get("images")
    logo = None
    if isinstance(images, Mapping):
        logo = _url(images.get("logo")) or _url(images.get("icon"))

    return BrandLook(
        colors=colors,
        fonts=fonts,
        logo_url=logo,
        color_scheme=scheme if scheme in ("light", "dark") else None,
        sources=[SOURCE],
        fetched_at=datetime.now(timezone.utc),
    )


def _hex(value: Any) -> Optional[str]:
    """A CSS hex colour, or nothing. Third-party text is never trusted raw."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) not in _HEX_LENGTHS or not text.startswith("#"):
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in text[1:]):
        return None
    return text.lower()


def _url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:2048] if text.startswith("https://") else None


__all__ = [
    "BrandLookUnavailable",
    "ENDPOINT",
    "SOURCE",
    "brand_look",
    "configured",
    "parse_branding",
]
