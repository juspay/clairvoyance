"""Versioned, vertical-agnostic artefacts that flow between engine stages (ASSIST-ENGINE-DESIGN.md §2).

Every artefact carries provenance (``sources`` / ``fetched_at``) so a slot,
a tile or a policy fact can always be traced to the page it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

InstallMethod = Literal["theme_embed", "snippet"]


class Signal(BaseModel):
    """One classifier observation: ``kind`` (script_src, cookie_key, js_literal,
    js_global, header, meta, path) + the matched ``pattern``."""

    kind: str
    pattern: str


class SiteProfile(BaseModel):
    """Stage 1 output: what one browser-like GET of ``/`` revealed."""

    url: str
    final_url: str
    status: int
    size_bytes: int = 0
    headers: Dict[str, str] = Field(default_factory=dict)
    cookies: List[str] = Field(default_factory=list)
    meta: Dict[str, str] = Field(default_factory=dict)
    link_rels: List[Dict[str, str]] = Field(default_factory=list)
    json_ld: List[Dict[str, Any]] = Field(default_factory=list)
    script_hosts: Dict[str, int] = Field(default_factory=dict)
    inline_literals: Dict[str, str] = Field(default_factory=dict)
    signals: List[Signal] = Field(default_factory=list)
    challenge: bool = False
    fetched_at: Optional[datetime] = None


class TenantIdentity(BaseModel):
    """Stage 2 output: who this store is to us."""

    platform: str
    canonical_host: str
    permanent_host: Optional[str] = None
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None


class SiteResearch(BaseModel):
    """Stage 3 output — the vertical-agnostic part; missing = null, never invented.

    A vertical extends this with its own inventory shapes (see the commerce
    vertical's ``assist/commerce/models.py``).
    """

    platform: str
    canonical_origin: str
    extra_origins: List[str] = Field(default_factory=list)
    name: Optional[str] = None
    brand_story: Optional[str] = None
    tone_hints: List[str] = Field(default_factory=list)
    audience: Optional[str] = None
    policies: Dict[str, str] = Field(default_factory=dict)
    contacts: Dict[str, str] = Field(default_factory=dict)
    offers: List[str] = Field(default_factory=list)
    logo_url: Optional[str] = None
    brand_colors: Dict[str, str] = Field(default_factory=dict)
    confidence: Dict[str, float] = Field(default_factory=dict)
    sources: List[str] = Field(default_factory=list)
    fetched_at: Optional[datetime] = None


class ResearchDelta(BaseModel):
    """What a platform adapter adds to the generic research; merged field by
    field, adapter wins. Vertical-specific payloads travel in ``vertical``
    for that vertical's merge step."""

    name: Optional[str] = None
    permanent_host: Optional[str] = None
    policies: Dict[str, str] = Field(default_factory=dict)
    contacts: Dict[str, str] = Field(default_factory=dict)
    brand: Dict[str, Any] = Field(default_factory=dict)
    extra_origins: List[str] = Field(default_factory=list)
    vertical: Dict[str, Any] = Field(default_factory=dict)
    sources: List[str] = Field(default_factory=list)


class ToolBinding(BaseModel):
    kind: Literal["mcp", "function"]
    name: str
    url: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class TemplateCandidate(BaseModel):
    """Stage 5 output, before the gate."""

    template_id: str
    name: str
    flow: Dict[str, Any]
    configurations: Dict[str, Any]
    expected_payload_schema: Dict[str, Any] = Field(default_factory=dict)
    supported_channels: List[str] = Field(default_factory=lambda: ["chat"])
    is_active: bool = False
    core_hash: Optional[str] = None


class WidgetBinding(BaseModel):
    widget_config_id: str
    public_widget_key: str
    allowed_origins: List[str] = Field(default_factory=list)
    appearance: Dict[str, Any] = Field(default_factory=dict)


class MirrorPolicy(BaseModel):
    """What the preview mirror may proxy for this platform.

    ``handoff`` is how a blocked transactional path sends the visitor to the
    real site: ``permalink`` rebuilds their in-progress state on the real
    domain, ``link`` opens the real URL, ``none`` shows only the notice.
    """

    blocked_paths: List[str] = Field(default_factory=list)
    allowed_xhr: List[str] = Field(default_factory=list)
    cdn_hosts: List[str] = Field(default_factory=list)
    script_policy: Literal["drop_third_party", "pass_through"] = "drop_third_party"
    handoff: Literal["permalink", "link", "none"] = "link"
    section_probe: bool = False


__all__ = [
    "InstallMethod",
    "MirrorPolicy",
    "ResearchDelta",
    "Signal",
    "SiteProfile",
    "SiteResearch",
    "TemplateCandidate",
    "TenantIdentity",
    "ToolBinding",
    "WidgetBinding",
]
