"""The prompt skeleton mechanism — markers, platform sections, slot normalization.

What a skeleton CONTAINS is a vertical's business: the commerce vertical
ships ``assist/commerce/skeleton.py`` (its slot phrases, its vertical-help
section); a booking or transit vertical ships its own. The engine only
knows the shape: a brand marker, an operating head, platform sections,
and a table of slot patterns to normalize when comparing cores.

A blueprint prompt is ``{{brand_identity_section}}`` followed by the shared
operating block. Runs of platform-only text are wrapped INLINE in
``{{#platform_section:<platform>}}…{{/platform_section}}`` so a build for
that platform is byte-identical to the block without markers, and a build
for any other platform drops the run. Adapters may also register the
legacy marker pair they shipped before this syntax existed; the engine
treats those exactly like the generic form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Pattern, Set, Tuple

BRAND_MARKER = "{{brand_identity_section}}"
OPERATING_HEAD = "## Operating principles"
SHOP_DOMAIN_PLACEHOLDER = "{{shop_domain}}"


@dataclass(frozen=True)
class SkeletonSpec:
    """A vertical's prompt skeleton: what varies per merchant inside a shared core.

    ``slot_patterns`` are (regex, replacement) pairs applied to the operating
    block before hashing, so two prompts built from the same skeleton with
    different merchant slots compare equal. ``vertical_section_end`` is the
    heading that closes the merchant-specific help section (its heading text
    varies per merchant, so the whole section is normalized away).
    """

    id: str
    brand_marker: str = BRAND_MARKER
    operating_head: str = OPERATING_HEAD
    vertical_section_end: str | None = None
    slot_patterns: Tuple[Tuple[Pattern[str], str], ...] = field(default_factory=tuple)


SECTION_END = "{{/platform_section}}"
_SECTION_START = re.compile(r"\{\{#platform_section:([a-z0-9_-]+)\}\}")

# start marker -> (platform id, end marker); adapters contribute their legacy pairs
LegacyMarkers = Mapping[str, Tuple[str, str]]


@dataclass(frozen=True)
class PlatformSection:
    platform: str
    start: int  # index of the start marker
    body_start: int  # first char after the start marker
    body_end: int  # index of the end marker
    end: int  # first char after the end marker


def platform_sections(
    prompt: str, legacy: LegacyMarkers | None = None
) -> List[PlatformSection]:
    """Every platform section in order; ``ValueError`` when malformed.

    Each start must be closed by its own end marker before the next start;
    an end marker without a start is a stray. Zero sections is legal — a
    blueprint for a platform without tools has nothing to wrap.
    """
    legacy = dict(legacy or {})
    starts: List[Tuple[int, int, str, str]] = []  # (index, len, platform, end marker)
    for match in _SECTION_START.finditer(prompt):
        starts.append((match.start(), len(match.group(0)), match.group(1), SECTION_END))
    for marker, (platform, end_marker) in legacy.items():
        position = 0
        while True:
            index = prompt.find(marker, position)
            if index < 0:
                break
            starts.append((index, len(marker), platform, end_marker))
            position = index + len(marker)
    starts.sort()

    sections: List[PlatformSection] = []
    cursor = 0
    for index, length, platform, end_marker in starts:
        if index < cursor:
            raise ValueError(f"nested platform section at {index}")
        body_start = index + length
        body_end = prompt.find(end_marker, body_start)
        if body_end < 0:
            raise ValueError(f"unterminated platform section {platform!r}")
        end = body_end + len(end_marker)
        sections.append(PlatformSection(platform, index, body_start, body_end, end))
        cursor = end

    expected_ends = len(sections)
    end_markers = {SECTION_END} | {end for _, end in legacy.values()}
    actual_ends = sum(prompt.count(marker) for marker in end_markers)
    if actual_ends != expected_ends:
        raise ValueError("stray platform section end marker")
    return sections


def resolve_platform_sections(
    prompt: str, keep: Set[str], legacy: LegacyMarkers | None = None
) -> str:
    """Strip the markers of kept platforms' sections; remove every other section."""
    resolved = prompt
    for section in reversed(platform_sections(prompt, legacy)):
        if section.platform in keep:
            resolved = (
                resolved[: section.start]
                + resolved[section.body_start : section.body_end]
                + resolved[section.end :]
            )
        else:
            resolved = resolved[: section.start] + resolved[section.end :]
    return resolved


def fill_placeholders(value: Any, mapping: Mapping[str, str]) -> Any:
    """Substitute every placeholder in every string of a blueprint value."""
    if isinstance(value, str):
        for placeholder, replacement in mapping.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [fill_placeholders(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: fill_placeholders(item, mapping) for key, item in value.items()}
    return value


__all__ = [
    "BRAND_MARKER",
    "OPERATING_HEAD",
    "PlatformSection",
    "SECTION_END",
    "SHOP_DOMAIN_PLACEHOLDER",
    "SkeletonSpec",
    "fill_placeholders",
    "platform_sections",
    "resolve_platform_sections",
]
