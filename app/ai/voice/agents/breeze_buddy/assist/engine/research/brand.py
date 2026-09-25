"""What a site looks like, so the assistant can start out looking like it.

Sources, most trusted first: the platform's own brand settings (the merchant
typed them), a rendered read of the page (Firecrawl), then the colours the
logo is made of. Each can be missing; whatever survives the guards is a
starting point the merchant can change.
"""

from __future__ import annotations

import asyncio
import colorsys
import io
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast
from urllib.parse import urljoin

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    ColorRole,
    SiteProfile,
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
PLATFORM_SOURCE = "platform:brand"

# A logo is small; anything bigger is not worth decoding.
MAX_LOGO_BYTES = 2 * 1024 * 1024
# Checked from the header, before a single pixel is decoded.
MAX_LOGO_PIXELS = 4_000_000
_LOGO_TIMEOUT_SECONDS = 10.0
_THUMBNAIL = (128, 128)
_QUANTISE_TO = 8
_OPAQUE_ALPHA = 200
# Only the formats logos ship in; every other Pillow decoder stays unreachable.
_LOGO_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "ICO")

# Below this saturation, or outside this lightness band, a colour is a grey,
# a near-white or a near-black: a surface, not an identity.
_MIN_SATURATION = 0.22
_LIGHTNESS_BAND = (0.10, 0.92)
# Closer than this to the page background, a candidate IS the background.
_BACKGROUND_DISTANCE = 48.0
_SAME_COLOUR_DISTANCE = 40.0

_SVG_COLOR = re.compile(
    r"(?:fill|stop-color|stroke)\s*[:=]\s*[\"']?(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?)\b",
    re.IGNORECASE,
)


def _rgb(value: str) -> Tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(value)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


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


def svg_colors(markup: str) -> List[str]:
    """Chromatic colours an SVG declares, most-used first."""
    tally: Dict[str, int] = {}
    for value in _SVG_COLOR.findall(markup):
        key = value.lower()
        tally[key] = tally.get(key, 0) + 1
    ranked = sorted(tally.items(), key=lambda entry: -entry[1])
    return [value for value, _ in ranked if is_chromatic(value)]


def _is_svg(raw: bytes, content_type: str) -> bool:
    if "svg" in content_type.lower():
        return True
    head = raw[:400].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


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
    except (UnsafeUrlError, FetchFailedError, EgressNotGuardedError) as exc:
        logger.info(f"assist brand: logo unreadable ({exc})")
        return []
    if response.status != 200 or response.truncated:
        return []
    if _is_svg(response.raw, response.headers.get("content-type", "")):
        return svg_colors(response.raw.decode("utf-8", errors="replace"))
    return await asyncio.to_thread(raster_colors, response.raw)


def from_logo(colours: Sequence[str], logo_url: Optional[str] = None) -> BrandLook:
    """A logo's palette as a brand look: strongest colour leads."""
    look = BrandLook(logo_url=logo_url, sources=[LOGO_SOURCE] if colours else [])
    roles: Tuple[ColorRole, ...] = ("primary", "secondary", "accent")
    for role, value in zip(roles, colours):
        look.colors.append(
            BrandColor(role=role, hex=value, source=LOGO_SOURCE, confidence=0.5)
        )
    return look


def from_platform(
    primary: Optional[str], secondary: Optional[str], logo_url: Optional[str] = None
) -> BrandLook:
    """The platform's own brand settings — the merchant set them, so they lead."""
    look = BrandLook(logo_url=logo_url)
    pairs: Tuple[Tuple[ColorRole, Optional[str]], ...] = (
        ("primary", primary),
        ("secondary", secondary),
    )
    for role, value in pairs:
        color = firecrawl.hex_color(value)
        if color:
            look.colors.append(
                BrandColor(
                    role=role, hex=color, source=PLATFORM_SOURCE, confidence=0.95
                )
            )
    if look.colors or look.logo_url:
        look.sources.append(PLATFORM_SOURCE)
    return look


def merge(*looks: Optional[BrandLook]) -> BrandLook:
    """Layer the sources in order: the first to name a role wins it; the rest
    are kept as alternates."""
    merged = BrandLook()
    for look in looks:
        if look is None:
            continue
        taken = {entry.role for entry in merged.colors}
        for entry in look.colors:
            (merged.alternates if entry.role in taken else merged.colors).append(entry)
        merged.logo_url = merged.logo_url or look.logo_url
        merged.alternates.extend(look.alternates)
        merged.sources.extend(look.sources)
        merged.warnings.extend(look.warnings)
    merged.sources = list(dict.fromkeys(merged.sources))
    return merged


def apply_guards(look: BrandLook, *, stock_colors: Sequence[str] = ()) -> BrandLook:
    """Set aside what cannot be a brand colour, then make sure there is a primary.

    Out: a platform's stock theme colours, the page background under another
    label, and greys. If that leaves no primary, the survivor that other
    sources agree with most is promoted.
    """
    background = look.color("background") or "#ffffff"
    stock = [value.lower() for value in stock_colors]
    kept: List[BrandColor] = []
    for entry in look.colors:
        reason = None
        if entry.role == "background":
            kept.append(entry)
            continue
        if any(distance(entry.hex, value) < _SAME_COLOUR_DISTANCE for value in stock):
            reason = "matches a stock theme colour"
        elif distance(entry.hex, background) < _BACKGROUND_DISTANCE:
            reason = "is the page background"
        elif not is_chromatic(entry.hex):
            reason = "is a grey or near-black"
        if reason:
            look.warnings.append(f"{entry.role} {entry.hex} {reason} — ignored")
            look.alternates.append(entry)
        else:
            kept.append(entry)
    look.colors = kept

    if look.color("primary") is None:
        promoted = _best_promotion(look)
        if promoted is not None:
            look.colors.append(
                BrandColor(
                    role="primary",
                    hex=promoted.hex,
                    source=f"{promoted.source} (promoted from {promoted.role})",
                    confidence=promoted.confidence * 0.8,
                )
            )
    return look


def _best_promotion(look: BrandLook) -> Optional[BrandColor]:
    candidates = [
        entry for entry in look.colors if entry.role in ("secondary", "accent", "link")
    ]
    if not candidates:
        return None
    others = [*look.alternates, *look.colors]

    def corroboration(entry: BrandColor) -> Tuple[int, float]:
        agreeing = sum(
            1
            for other in others
            if other.source != entry.source
            and distance(entry.hex, other.hex) < _SAME_COLOUR_DISTANCE
        )
        return agreeing, entry.confidence

    return max(candidates, key=corroboration)


async def resolve(
    url: str,
    profile: SiteProfile,
    *,
    platform_look: Optional[BrandLook] = None,
    stock_colors: Sequence[str] = (),
) -> BrandLook:
    """Gather every source, layer them, guard the result. Never raises for a
    missing source: each one that fails leaves a warning instead."""
    warnings: List[str] = []
    provider_look: Optional[BrandLook] = None
    try:
        provider_look = await firecrawl.brand_look(url)
    except firecrawl.BrandProviderNotConfiguredError as exc:
        logger.error(f"assist brand: {exc}")
        warnings.append("brand provider not configured — skipped")
    except (firecrawl.BrandLookUnavailable, UnsafeUrlError) as exc:
        logger.info(f"assist brand: provider unavailable ({exc})")
        warnings.append("brand provider unavailable — skipped")

    # Only a named logo is kept as the logo; a banner or icon is sampled for
    # colour and then dropped.
    logo_url = (
        (platform_look.logo_url if platform_look else None)
        or (provider_look.logo_url if provider_look else None)
        or next(iter(named_logos(profile)), None)
    )
    sampled = logo_url or next(iter(logo_candidates(profile)), None)
    logo_look = from_logo(await logo_colors(sampled), logo_url) if sampled else None

    merged = merge(platform_look, provider_look, logo_look)
    merged.warnings = [*warnings, *merged.warnings]
    if not merged.colors:
        merged.warnings.append("no brand colour could be established")
    return apply_guards(merged, stock_colors=stock_colors)


__all__ = [
    "LOGO_SOURCE",
    "MAX_LOGO_BYTES",
    "MAX_LOGO_PIXELS",
    "PLATFORM_SOURCE",
    "apply_guards",
    "distance",
    "from_logo",
    "from_platform",
    "is_chromatic",
    "logo_candidates",
    "logo_colors",
    "merge",
    "named_logos",
    "raster_colors",
    "resolve",
    "svg_colors",
]
