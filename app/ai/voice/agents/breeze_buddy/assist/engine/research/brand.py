"""What a site looks like, so the assistant can start out looking like it.

Sources, most trusted first: a rendered read of the page (Firecrawl), then the
colours the logo is made of. Either can be missing; whatever survives the
guards is a starting point the merchant can change.
"""

from __future__ import annotations

import asyncio
import colorsys
import io
import math
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast
from urllib.parse import urljoin

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    ColorRole,
    SiteProfile,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

LOGO_SOURCE = "logo"

# Largest logo downloaded; anything bigger is not worth decoding.
MAX_LOGO_BYTES = 2 * 1024 * 1024
# Largest logo decoded, checked from the header before any pixel is.
MAX_LOGO_PIXELS = 4_000_000
# Longest wait for the logo download.
_LOGO_TIMEOUT_SECONDS = 10.0
# Size a logo is shrunk to before its colours are counted.
_THUMBNAIL = (128, 128)
# Colours a logo is reduced to.
_QUANTISE_TO = 8
# Alpha above which a pixel counts as part of the logo, not its background.
_OPAQUE_ALPHA = 200
# Only the formats logos ship in; every other Pillow decoder stays unreachable.
_LOGO_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "ICO")

# Below this saturation, or outside this lightness band, a colour is a grey,
# a near-white or a near-black: a surface, not an identity.
_MIN_SATURATION = 0.22
_LIGHTNESS_BAND = (0.10, 0.92)
# Lighter than this, a colour still reads as a colour but cannot carry text:
# a blush pink under white type is unreadable, so it is no brand accent.
_TOO_PALE = 0.82
# Darker than this, a colour is black enough to be a black-and-white store's
# brand colour when nothing else is.
_DARK_ENOUGH = 0.25
# Distances are straight-line RGB (0 to 441). Closer than this to the page
# background, a candidate IS the background.
_BACKGROUND_DISTANCE = 48.0
# Closer than this, two colours are the same colour.
_SAME_COLOUR_DISTANCE = 40.0

_SVG_COLOR = re.compile(
    r"(?:fill|stop-color|stroke)\s*[:=]\s*[\"']?(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?)\b",
    re.IGNORECASE,
)


async def resolve(
    url: str,
    profile: SiteProfile,
    *,
    stock_colors: Sequence[str] = (),
    deadline: Optional[float] = None,
) -> BrandLook:
    """Gather every source, layer them, guard the result. Never raises for a
    missing source: each one that fails leaves a warning instead.

    ``deadline`` (a ``time.monotonic()`` value) is when the caller stops
    waiting. The render gets only what is left after the logo's share, so a
    slow render costs that one source, not the whole look.
    """
    warnings: List[str] = []
    provider_look = await _provider_look(url, deadline, warnings)
    background = provider_look.color("background") if provider_look else None

    # Only a named logo is kept as the logo; a banner or icon is sampled for
    # colour and then dropped.
    logo_url = (provider_look.logo_url if provider_look else None) or next(
        iter(named_logos(profile)), None
    )
    sampled = logo_url or next(iter(logo_candidates(profile)), None)
    logo_look = from_logo(await logo_colors(sampled), logo_url) if sampled else None

    # Each source is guarded on its own, so a colour set aside from one does
    # not keep the other's colour out of that role.
    guarded = [
        apply_guards(look, background=background, stock_colors=stock_colors)
        for look in (provider_look, logo_look)
        if look is not None
    ]
    merged = promote_primary(merge(*guarded))
    merged.warnings = warnings + [note for look in guarded for note in look.warnings]
    if merged.color("primary") is None:
        merged = monochrome_primary(merged, [provider_look, logo_look])
    if not any(entry.role != "background" for entry in merged.colors):
        merged.warnings.append("no brand colour could be established")
    merged.icon_url = (provider_look.icon_url if provider_look else None) or next(
        iter(site_icons(profile)), None
    )
    return merged


async def _provider_look(
    url: str, deadline: Optional[float], warnings: List[str]
) -> Optional[BrandLook]:
    timeout = firecrawl.DEFAULT_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic() - _LOGO_TIMEOUT_SECONDS)
    if timeout <= 0:
        warnings.append("brand provider skipped — no time left")
        return None
    try:
        return await firecrawl.brand_look(url, timeout_seconds=timeout)
    except WebsiteScrapingConfigurationError as exc:
        logger.warning(f"assist brand: {exc}")
        warnings.append("brand provider not configured — skipped")
    except (WebsiteScrapingUpstreamError, UnsafeUrlError) as exc:
        logger.info(f"assist brand: provider unavailable ({exc})")
        warnings.append("brand provider unavailable — skipped")
    return None


def named_logos(profile: SiteProfile) -> List[str]:
    """Images the page itself calls its logo (structured data, ``og:logo``) —
    the only page-sourced images safe to show as one."""
    found: List[str] = []
    for block in profile.json_ld:
        logo = block.get("logo") if isinstance(block, dict) else None
        if isinstance(logo, dict):
            logo = logo.get("url")
        if isinstance(logo, str):
            found.append(logo)
    if profile.meta.get("og:logo"):
        found.append(profile.meta["og:logo"])
    return _absolute_https(profile, found)


def logo_candidates(profile: SiteProfile) -> List[str]:
    """Every image worth reading colours from, best first.

    Adds the ``og:image`` (often a banner) and the icons after the named
    logos: fine to sample for colour, not to show as a logo.
    """
    found = list(named_logos(profile))
    if profile.meta.get("og:image"):
        found.append(profile.meta["og:image"])
    for link in profile.link_rels:
        if "icon" in link.get("rel", "").lower() and link.get("href"):
            found.append(link["href"])
    return _absolute_https(profile, found)


def _absolute_https(profile: SiteProfile, found: List[str]) -> List[str]:
    base = profile.final_url or profile.url
    absolute = [urljoin(base, value.strip()) for value in found]
    return list(dict.fromkeys(url for url in absolute if url.startswith("https://")))


async def logo_colors(url: str) -> List[str]:
    """The chromatic colours of the logo at ``url``, via the guarded fetcher."""
    try:
        response = await fetch_page(
            url,
            max_bytes=MAX_LOGO_BYTES,
            timeout_seconds=_LOGO_TIMEOUT_SECONDS,
            headers={"Accept": "image/*"},
            decode=False,
        )
    except (
        UnsafeUrlError,
        FetchFailedError,
        EgressNotGuardedError,
        aiohttp.ClientError,
        asyncio.TimeoutError,
    ) as exc:
        # A body that fails partway surfaces as aiohttp's own error.
        logger.info(f"assist brand: logo unreadable ({exc!r})")
        return []
    if response.status != 200 or response.truncated:
        return []
    if _is_svg(response.raw, response.headers.get("content-type", "")):
        return svg_colors(response.raw.decode("utf-8", errors="replace"))
    return await asyncio.to_thread(raster_colors, response.raw)


def _is_svg(raw: bytes, content_type: str) -> bool:
    if "svg" in content_type.lower():
        return True
    head = raw[:400].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def svg_colors(markup: str) -> List[str]:
    """Chromatic colours an SVG declares, most-used first."""
    tally: Dict[str, int] = {}
    for value in _SVG_COLOR.findall(markup):
        key = value.lower()
        tally[key] = tally.get(key, 0) + 1
    ranked = sorted(tally.items(), key=lambda entry: -entry[1])
    return [value for value, _ in ranked if is_chromatic(value)]


def raster_colors(raw: bytes) -> List[str]:
    """Chromatic colours a raster logo is made of, most-used first.

    Blocking and CPU-bound, so callers run it in a thread. The pixel count is
    read from the header and refused before decoding; the image is shrunk
    before it is converted, so only a thumbnail is ever expanded to RGBA.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw), formats=_LOGO_FORMATS) as image:
            width, height = image.size
            if width * height > MAX_LOGO_PIXELS:
                return []
            image.draft("RGB", _THUMBNAIL)
            image.thumbnail(_THUMBNAIL)
            rgba = image.convert("RGBA")
        opaque = [
            pixel[:3] for pixel in cast(Any, rgba.getdata()) if pixel[3] > _OPAQUE_ALPHA
        ]
        if not opaque:
            return []
        flat = Image.new("RGB", (len(opaque), 1))
        flat.putdata(opaque)
        reduced = flat.quantize(colors=_QUANTISE_TO, method=Image.Quantize.MEDIANCUT)
        palette = list(reduced.getpalette() or [])
        ordered = sorted(cast(Any, reduced.getcolors()) or [], reverse=True)
    except Exception as exc:  # any decode failure just means "no logo colours"
        logger.info(f"assist brand: logo not decodable ({type(exc).__name__})")
        return []

    colours: List[str] = []
    for _count, index in ordered:
        red, green, blue = palette[index * 3 : index * 3 + 3]
        value = f"#{red:02x}{green:02x}{blue:02x}"
        if is_chromatic(value):
            colours.append(value)
    return colours


def from_logo(colours: Sequence[str], logo_url: Optional[str] = None) -> BrandLook:
    """A logo's palette as a brand look: strongest colour leads."""
    look = BrandLook(logo_url=logo_url)
    roles: Tuple[ColorRole, ...] = ("primary", "secondary", "accent")
    for role, value in zip(roles, colours):
        look.colors.append(BrandColor(role=role, hex=value, source=LOGO_SOURCE))
    return look


def merge(*looks: Optional[BrandLook]) -> BrandLook:
    """Layer the sources in order: the first to name a role wins it."""
    merged = BrandLook()
    for look in looks:
        if look is None:
            continue
        taken = {entry.role for entry in merged.colors}
        merged.colors.extend(entry for entry in look.colors if entry.role not in taken)
        merged.logo_url = merged.logo_url or look.logo_url
    return merged


def apply_guards(
    look: BrandLook,
    *,
    background: Optional[str] = None,
    stock_colors: Sequence[str] = (),
) -> BrandLook:
    """A copy of ``look`` without what cannot be a brand colour.

    Out: a platform's stock theme colours, the page background under another
    label (``background``, else the look's own, else white), and greys. Each
    one set aside is added to the copy's warnings.
    """
    background = background or look.color("background") or "#ffffff"
    kept: List[BrandColor] = []
    warnings = list(look.warnings)
    for entry in look.colors:
        reason = None
        if entry.role == "background":
            kept.append(entry)
            continue
        if any(
            distance(entry.hex, value) < _SAME_COLOUR_DISTANCE for value in stock_colors
        ):
            reason = "matches a stock theme colour"
        elif distance(entry.hex, background) < _BACKGROUND_DISTANCE:
            reason = "is the page background"
        elif not is_chromatic(entry.hex):
            reason = "is a grey, near-white or near-black"
        elif _lightness(entry.hex) > _TOO_PALE:
            reason = "is too pale to carry text"
        if reason:
            warnings.append(f"{entry.role} {entry.hex} {reason} — ignored")
        else:
            kept.append(entry)
    return look.model_copy(update={"colors": kept, "warnings": warnings})


def promote_primary(look: BrandLook) -> BrandLook:
    """``look``, or a copy where the first secondary, accent or link colour
    stands in for a missing primary."""
    if look.color("primary") is not None:
        return look
    stand_in = next(
        (e for e in look.colors if e.role in ("secondary", "accent", "link")), None
    )
    if stand_in is None:
        return look
    primary = BrandColor(
        role="primary",
        hex=stand_in.hex,
        source=f"{stand_in.source} (promoted from {stand_in.role})",
    )
    return look.model_copy(update={"colors": [*look.colors, primary]})


def monochrome_primary(
    look: BrandLook, sources: Sequence[Optional[BrandLook]]
) -> BrandLook:
    """``look`` with the darkest near-black any source named as its primary.

    A black-and-white store (black header, black buttons, white type) has no
    chromatic colour, and its identity IS the black; offering nothing, or a
    pale accent from one banner, reads as another brand.
    """
    dark = sorted(
        (
            entry
            for source in sources
            if source is not None
            for entry in source.colors
            if _lightness(entry.hex) < _DARK_ENOUGH
        ),
        key=lambda entry: _lightness(entry.hex),
    )
    if not dark:
        return look
    primary = BrandColor(
        role="primary",
        hex=dark[0].hex,
        source=f"{dark[0].source} (black and white store)",
    )
    return look.model_copy(update={"colors": [*look.colors, primary]})


def site_icons(profile: SiteProfile) -> List[str]:
    """The page's own square icons, the phone-sized one first."""
    links = [link for link in profile.link_rels if link.get("href")]
    touch = [
        link["href"]
        for link in links
        if "apple-touch-icon" in link.get("rel", "").lower()
    ]
    icons = [link["href"] for link in links if "icon" in link.get("rel", "").lower()]
    return _absolute_https(profile, touch + icons)


def _lightness(value: str) -> float:
    try:
        red, green, blue = (channel / 255 for channel in _rgb(value))
    except ValueError:
        return 1.0
    return colorsys.rgb_to_hls(red, green, blue)[1]


def is_chromatic(value: str) -> bool:
    """Does this colour carry identity, or is it just a surface?"""
    try:
        red, green, blue = (channel / 255 for channel in _rgb(value))
    except ValueError:
        return False
    _, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return (
        saturation > _MIN_SATURATION
        and _LIGHTNESS_BAND[0] < lightness < _LIGHTNESS_BAND[1]
    )


def distance(first: str, second: str) -> float:
    try:
        one, two = _rgb(first), _rgb(second)
    except ValueError:
        return math.inf
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(one, two)))


def _rgb(value: str) -> Tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(value)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


__all__ = ["resolve"]
