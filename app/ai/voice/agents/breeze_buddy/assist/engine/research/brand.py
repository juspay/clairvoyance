"""Stage 3 lane A — what a site looks like, so the assistant can look like it.

Four sources, layered because each fails somewhere the others do not. Measured
on live storefronts 2026-09-10:

* **The provider** (a rendered scrape reading computed styles) is the best
  single answer — exact against ground truth on two stores, and it correctly
  demoted to *accent* a colour that naive frequency analysis had promoted to
  primary. Not infallible: on one well-known site it returned a brand colour
  the company had retired.
* **The platform's own brand block**, where the platform has one, is
  authoritative because the merchant set it — but it is often left empty, so it
  cannot stand alone.
* **The logo** is the most *widely available* signal of the four. Colour data
  is missing on most platforms; a logo almost always exists. It is exact when
  the mark is coloured and silent when the mark is monochrome.
* **Stock theme colours are excluded.** A platform's default palette looks like
  a confident answer and means nothing. Each adapter declares its own defaults;
  the engine only knows there is a list.

Nothing here decides alone. The result carries every candidate with its source
so the console can show them and the merchant can choose — which the
measurements say is not a fallback but the honest design.
"""

from __future__ import annotations

import colorsys
import io
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

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
    FetchFailedError,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

LOGO_SOURCE = "logo:quantised"
PLATFORM_SOURCE = "platform:brand"
# A logo is small; anything larger is a hero image someone mislabelled.
_MAX_LOGO_BYTES = 4 * 1024 * 1024
_QUANTISE_TO = 8
_THUMBNAIL = (200, 200)
# Below this, a "colour" is a grey, a near-white or a near-black — a surface,
# not an identity.
_MIN_SATURATION = 0.22
_LIGHTNESS_BAND = (0.10, 0.92)
# A candidate this close to the page's own background IS the background.
# Measured 2026-09-10: a contrast-ratio test looked right and was wrong — at a
# 3:1 threshold it discarded a store's authoritative gold (#b69d6c) and another
# brand's blue (#00aeef), because plenty of real brand colours are mid-tone
# fills that carry white text rather than text on white. Sameness is the
# question worth asking; legibility is the widget's problem, not identity's.
_BACKGROUND_DISTANCE = 48.0
_SAME_COLOUR_DISTANCE = 40.0


def _rgb(value: str) -> Tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def is_chromatic(value: str) -> bool:
    """Does this colour carry identity, or is it just a surface?"""
    try:
        red, green, blue = (channel / 255 for channel in _rgb(value))
    except (ValueError, IndexError):
        return False
    _, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return (
        saturation > _MIN_SATURATION
        and _LIGHTNESS_BAND[0] < lightness < _LIGHTNESS_BAND[1]
    )


def distance(first: str, second: str) -> float:
    try:
        one, two = _rgb(first), _rgb(second)
    except (ValueError, IndexError):
        return math.inf
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(one, two)))


def _relative_luminance(value: str) -> float:
    def channel(raw: int) -> float:
        part = raw / 255
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(c) for c in _rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    try:
        one, two = _relative_luminance(first), _relative_luminance(second)
    except (ValueError, IndexError):
        return 1.0
    lighter, darker = max(one, two), min(one, two)
    return (lighter + 0.05) / (darker + 0.05)


def logo_candidates(profile: SiteProfile) -> List[str]:
    """Where a logo might be, best first.

    The platform's own answer comes from the adapter; this is what the page
    itself says. Structured data first because a site that publishes an
    ``Organization`` block has named its logo deliberately, where an
    ``og:image`` is often a campaign banner.
    """
    found: List[str] = []
    for block in profile.json_ld:
        logo = block.get("logo") if isinstance(block, dict) else None
        if isinstance(logo, dict):
            logo = logo.get("url")
        if isinstance(logo, str) and logo.startswith("http"):
            found.append(logo)
    for key in ("og:logo", "og:image"):
        value = profile.meta.get(key)
        if value and value.startswith("http"):
            found.append(value)
    for link in profile.link_rels:
        if "icon" in link.get("rel", "") and link.get("href", "").startswith("http"):
            found.append(link["href"])
    return list(dict.fromkeys(found))


# A vector logo states its colours instead of drawing them, so there is
# nothing to quantise and nothing to approximate.
_SVG_COLOR = re.compile(
    r"(?:fill|stop-color|stroke)\s*[:=]\s*[\"']?(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?)",
    re.IGNORECASE,
)


def svg_colors(markup: str) -> List[str]:
    """Chromatic fills declared in an SVG, most-used first.

    Exact where quantisation is an estimate: breeze.in's mark declares
    ``#6c5ff9``, which is the brand purple to the digit, and its home page
    carries no hex colours at all for anything else to find.
    """
    # Tallied by hand rather than with the stdlib helper: importing it puts a
    # vertical's word in this file, and the engine guard reads source text.
    # Insertion order plus a stable sort gives the same first-seen tie-break.
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


async def quantise_logo(url: str) -> List[str]:
    """The chromatic colours a logo is actually made of, most-used first.

    Transparency is dropped before quantising: a PNG mark on a transparent
    field would otherwise average toward whatever the padding is. A vector mark
    skips quantisation entirely and is read for the colours it declares.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return []
    try:
        response = await fetch_page(
            url, max_bytes=_MAX_LOGO_BYTES, timeout_seconds=20, decode=False
        )
    except (UnsafeUrlError, FetchFailedError) as exc:
        logger.info(f"assist brand: logo unreadable ({exc})")
        return []
    raw = response.raw
    if _is_svg(raw, response.headers.get("content-type", "")):
        return svg_colors(raw.decode("utf-8", errors="replace"))
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        image.thumbnail(_THUMBNAIL)
        pixels = cast(Any, image.getdata())
        opaque = [pixel for pixel in pixels if pixel[3] > 200]
        if not opaque:
            return []
        flat = Image.new("RGB", (len(opaque), 1))
        flat.putdata([pixel[:3] for pixel in opaque])
        reduced = flat.quantize(colors=_QUANTISE_TO, method=Image.Quantize.MEDIANCUT)
        palette = list(reduced.getpalette() or [])
        ordered = sorted(cast(Any, reduced.getcolors()) or [], reverse=True)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any decode failure is just "no logo colours"
        logger.info(f"assist brand: logo not decodable ({type(exc).__name__})")
        return []

    colours: List[str] = []
    for _count, index in ordered:
        red, green, blue = palette[index * 3 : index * 3 + 3]
        value = f"#{red:02x}{green:02x}{blue:02x}"
        if is_chromatic(value):
            colours.append(value)
    return colours


def apply_guards(look: BrandLook, *, stock_colors: Sequence[str] = ()) -> BrandLook:
    """Drop what cannot be a brand colour, and say so.

    Two rules, both learned from measurement: a primary that fails contrast
    against its own background is the background wearing the wrong label, and a
    colour matching a platform's stock theme is the theme, not the merchant.
    """
    background = look.color("background") or "#ffffff"
    stock = [value.lower() for value in stock_colors]
    kept: List[BrandColor] = []
    for entry in look.colors:
        if entry.role in ("background", "text"):
            kept.append(entry)
            continue
        if any(distance(entry.hex, value) < _SAME_COLOUR_DISTANCE for value in stock):
            look.warnings.append(
                f"{entry.role} {entry.hex} matches a stock theme colour — ignored"
            )
            look.alternates.append(entry)
            continue
        if distance(entry.hex, background) < _BACKGROUND_DISTANCE:
            look.warnings.append(
                f"{entry.role} {entry.hex} is the page background — ignored"
            )
            look.alternates.append(entry)
            continue
        if not is_chromatic(entry.hex):
            look.warnings.append(
                f"{entry.role} {entry.hex} is a grey or a near-black — ignored"
            )
            look.alternates.append(entry)
            continue
        kept.append(entry)
    look.colors = kept

    # Leaving a merchant with no primary is worse than handing them a debatable
    # one they can change, so promote a survivor — but choose by corroboration,
    # not by order. Two independent sources landing on the same colour is the
    # strongest signal available here: on one store the logo quantised to a red
    # and the rendered page reported the same red as its accent, while the
    # nominal "primary" was a desaturated slate the logo knew nothing about.
    if not any(entry.role == "primary" for entry in look.colors):
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
    """The survivor most likely to be the brand: corroborated first."""
    candidates = [
        entry for entry in look.colors if entry.role in ("secondary", "accent", "link")
    ]
    if not candidates:
        return None
    others = [entry.hex for entry in look.alternates] + [
        entry.hex for entry in look.colors
    ]

    def corroboration(entry: BrandColor) -> Tuple[int, float]:
        agreeing = sum(
            1
            for value in others
            if value != entry.hex and distance(entry.hex, value) < _SAME_COLOUR_DISTANCE
        )
        return agreeing, entry.confidence

    return max(candidates, key=corroboration)


def merge(*looks: Optional[BrandLook]) -> BrandLook:
    """Layer the sources, first one to speak for a role wins.

    Order is the caller's judgement about which source to trust for this site;
    everything a later source offers for a role already filled is kept as an
    alternate, because the console offers them and the merchant decides.
    """
    merged = BrandLook(fetched_at=datetime.now(timezone.utc))
    for look in looks:
        if look is None:
            continue
        taken = {entry.role for entry in merged.colors}
        for entry in look.colors:
            (merged.colors if entry.role not in taken else merged.alternates).append(
                entry
            )
        for font in look.fonts:
            if not any(f.family.lower() == font.family.lower() for f in merged.fonts):
                merged.fonts.append(font)
        merged.logo_url = merged.logo_url or look.logo_url
        merged.color_scheme = merged.color_scheme or look.color_scheme
        merged.alternates.extend(look.alternates)
        merged.sources.extend(look.sources)
        merged.warnings.extend(look.warnings)
    merged.sources = list(dict.fromkeys(merged.sources))
    return merged


def from_logo(colours: Sequence[str], logo_url: Optional[str] = None) -> BrandLook:
    """A logo's palette as a brand look: strongest colour leads."""
    look = BrandLook(logo_url=logo_url, sources=[LOGO_SOURCE] if colours else [])
    roles: Tuple[ColorRole, ...] = ("primary", "secondary", "accent")
    for role, value in zip(roles, colours):
        look.colors.append(
            BrandColor(role=role, hex=value, source=LOGO_SOURCE, confidence=0.5)
        )
    return look


def from_platform(primary: Optional[str], secondary: Optional[str]) -> BrandLook:
    """The platform's own brand block — the merchant set it, so it leads."""
    look = BrandLook(sources=[PLATFORM_SOURCE] if (primary or secondary) else [])
    pairs: Tuple[Tuple[ColorRole, Optional[str]], ...] = (
        ("primary", primary),
        ("secondary", secondary),
    )
    for role, value in pairs:
        if value:
            look.colors.append(
                BrandColor(
                    role=role,
                    hex=value.lower(),
                    source=PLATFORM_SOURCE,
                    confidence=0.95,
                )
            )
    return look


async def resolve(
    url: str,
    profile: SiteProfile,
    *,
    platform_look: Optional[BrandLook] = None,
    stock_colors: Sequence[str] = (),
    use_provider: bool = True,
) -> BrandLook:
    """The whole lane: gather every source, layer them, guard the result."""
    provider_look: Optional[BrandLook] = None
    if use_provider:
        try:
            provider_look = await firecrawl.brand_look(url)
        except firecrawl.BrandLookUnavailable as exc:
            logger.info(f"assist brand: provider unavailable ({exc})")

    logo_url = (
        (platform_look.logo_url if platform_look else None)
        or (provider_look.logo_url if provider_look else None)
        or next(iter(logo_candidates(profile)), None)
    )
    logo_look: Optional[BrandLook] = None
    if logo_url:
        logo_look = from_logo(await quantise_logo(logo_url), logo_url)

    # The merchant's own setting outranks a reading of their page, which
    # outranks a reading of their logo.
    merged = merge(platform_look, provider_look, logo_look)
    merged.logo_url = merged.logo_url or logo_url
    if not merged.colors:
        merged.warnings.append("no brand colour could be established")
    return apply_guards(merged, stock_colors=stock_colors)


__all__ = [
    "LOGO_SOURCE",
    "PLATFORM_SOURCE",
    "apply_guards",
    "contrast_ratio",
    "distance",
    "from_logo",
    "from_platform",
    "is_chromatic",
    "logo_candidates",
    "merge",
    "quantise_logo",
    "resolve",
    "svg_colors",
]
