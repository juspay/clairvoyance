"""Brand colours and logo from a rendered page, via Firecrawl's ``branding`` format.

The browser's computed styles for the header and primary button name a brand
colour far more reliably than counting hex values in a stylesheet. A render
takes 15-40 s, so this only runs inside onboarding's budgeted brand step.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    ColorRole,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    normalize_probe_url,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.transport.http_client import create_aiohttp_session

ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
SOURCE = "firecrawl:branding"
DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_URL_LENGTH = 2048

# Their field name → our role. Anything unlisted is ignored.
_COLOR_ROLES: Mapping[str, ColorRole] = {
    "primary": "primary",
    "secondary": "secondary",
    "accent": "accent",
    "background": "background",
    "link": "link",
}
_HEX_DIGITS = frozenset("0123456789abcdef")


class BrandProviderNotConfiguredError(RuntimeError):
    """``FIRECRAWL_API_KEY`` is not set, so the provider refuses to run."""


class BrandLookUnavailable(RuntimeError):
    """The provider was called and could not answer."""


async def brand_look(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> BrandLook:
    """Render ``url`` and read the brand colours and logo the browser computed."""
    if not FIRECRAWL_API_KEY:
        raise BrandProviderNotConfiguredError(
            "FIRECRAWL_API_KEY is not set; brand detection via Firecrawl is off"
        )
    payload = {"url": normalize_probe_url(url), "formats": ["branding"]}
    headers = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
    try:
        async with create_aiohttp_session(
            timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as session:
            async with session.post(ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise BrandLookUnavailable(f"provider returned {resp.status}")
                parsed = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise BrandLookUnavailable(f"provider unreachable: {exc}") from exc
    except ValueError as exc:
        raise BrandLookUnavailable("provider returned invalid JSON") from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    branding = data.get("branding") if isinstance(data, dict) else None
    if not isinstance(branding, dict):
        raise BrandLookUnavailable("provider returned no branding block")
    return parse_branding(branding)


def parse_branding(branding: Dict[str, Any]) -> BrandLook:
    """Provider payload → ``BrandLook``. Pure, so a captured payload replays."""
    colors: List[BrandColor] = []
    raw_colors = branding.get("colors")
    if isinstance(raw_colors, Mapping):
        for field, role in _COLOR_ROLES.items():
            value = hex_color(raw_colors.get(field))
            if value:
                colors.append(
                    BrandColor(role=role, hex=value, source=SOURCE, confidence=0.8)
                )

    logo = None
    images = branding.get("images")
    if isinstance(images, Mapping):
        logo = _https_url(images.get("logo")) or _https_url(images.get("icon"))
    return BrandLook(colors=colors, logo_url=logo, sources=[SOURCE])


def hex_color(value: Any) -> Optional[str]:
    """A ``#rgb`` / ``#rrggbb`` colour, lower-cased, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) not in (4, 7) or not text.startswith("#"):
        return None
    return text if set(text[1:]) <= _HEX_DIGITS else None


def _https_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("https://") or len(text) > _MAX_URL_LENGTH:
        return None
    return text


__all__ = [
    "BrandLookUnavailable",
    "BrandProviderNotConfiguredError",
    "ENDPOINT",
    "SOURCE",
    "brand_look",
    "hex_color",
    "parse_branding",
]
