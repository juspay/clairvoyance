"""The platform registry — the engine's only door to a platform.

Adding a platform is a package under ``platforms/`` plus one entry here.
Stages call ``resolve`` / ``classify`` / ``for_request`` / ``for_host_app``;
they never import an adapter module, and they never name a platform.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Sequence, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal
from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import LegacyMarkers
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import PlatformAdapter
from app.ai.voice.agents.breeze_buddy.assist.platforms.generic.adapter import (
    adapter as generic,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.adapter import (
    adapter as shopify,
)

# Order = classification priority among equal scores. ``generic`` is last and
# scores zero by construction, so it only wins when nothing else matches.
PLATFORMS: Tuple[PlatformAdapter, ...] = (shopify, generic)
CLASSIFY_THRESHOLD = 0.5


def resolve(adapter_id: str) -> PlatformAdapter:
    for adapter in PLATFORMS:
        if adapter.id == adapter_id:
            return adapter
    raise KeyError(f"unknown platform adapter: {adapter_id!r}")


def for_request(platform: Optional[str]) -> PlatformAdapter:
    """The adapter behind an onboarding request's ``platform`` value."""
    for adapter in PLATFORMS:
        if adapter.request_platform == platform:
            return adapter
    return generic


def for_host_app(host_app: str) -> PlatformAdapter:
    """The adapter an install-time caller (a host app) lands on."""
    for adapter in PLATFORMS:
        if host_app in adapter.host_apps:
            return adapter
    raise KeyError(f"no platform adapter for host app {host_app!r}")


def classify(signals: Sequence[Signal]) -> Tuple[PlatformAdapter, float]:
    """(adapter, confidence) for a probe's signals; ``generic`` below threshold."""
    best, best_score = generic, 0.0
    for adapter in PLATFORMS:
        score = adapter.classify(signals)
        if score > best_score:
            best, best_score = adapter, score
    if best_score < CLASSIFY_THRESHOLD:
        return generic, best_score
    return best, best_score


def legacy_section_markers() -> LegacyMarkers:
    """Every legacy marker pair any platform still ships in its blueprints."""
    merged: Dict[str, Tuple[str, str]] = {}
    for adapter in PLATFORMS:
        merged.update(adapter.legacy_section_markers())
    return merged


def foreign_mcp_server_names(adapter: PlatformAdapter) -> FrozenSet[str]:
    """MCP server names owned by OTHER platforms — a build drops those."""
    names: set[str] = set()
    for other in PLATFORMS:
        if other.id != adapter.id:
            names |= set(other.mcp_server_names())
    return frozenset(names - set(adapter.mcp_server_names()))


def foreign_tool_config_keys(adapter: PlatformAdapter) -> Tuple[str, ...]:
    keys = [
        k
        for other in PLATFORMS
        if other.id != adapter.id
        for k in other.tool_config_keys()
    ]
    return tuple(dict.fromkeys(k for k in keys if k not in adapter.tool_config_keys()))


def foreign_payload_keys(adapter: PlatformAdapter) -> Tuple[str, ...]:
    keys = [
        k for other in PLATFORMS if other.id != adapter.id for k in other.payload_keys()
    ]
    return tuple(dict.fromkeys(k for k in keys if k not in adapter.payload_keys()))


__all__ = [
    "CLASSIFY_THRESHOLD",
    "PLATFORMS",
    "classify",
    "for_host_app",
    "for_request",
    "foreign_mcp_server_names",
    "foreign_payload_keys",
    "foreign_tool_config_keys",
    "legacy_section_markers",
    "resolve",
]
