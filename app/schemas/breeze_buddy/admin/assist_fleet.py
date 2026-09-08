"""Response models for ``GET /admin/assist-fleet``.

One payload describes the whole Buddy Assist fleet: one row per widget_config
(the agent's public surface) with its merchant, bound template and usage;
every chat-agent template under the assist resellers (bound or orphaned);
the shared-prompt-block variants; and the findings an operator should act on.
Bodies (``flow`` / ``configurations``) are never included — a diff view fetches
the two templates it compares through ``GET /templates/{id}``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AgentType = Literal["chat", "voice"]
Generation = Literal[
    "standard",  # shared block matches the reference (blueprint / configured)
    "variant",  # sectioned prompt, shared block differs from the reference
    "legacy-personalized",  # no sections, long prompt (old nautilus personalization)
    "bare",  # no sections, short default prompt
    "voice-agent",  # no 'chat' channel — telephony template, not graded
    "other-tenant",  # widget tenant outside the assist resellers, not graded
]
FlagLevel = Literal["critical", "warning", "info"]
HostApp = Literal["breeze-buddy", "buddy-assist", "direct"]
Platform = Literal["shopify", "woocommerce", "demo", "internal", "custom"]
CleanupAction = Literal["delete", "deactivate", "keep", "none"]


class FleetUsage(BaseModel):
    """Chat-session usage for one template (the widget's bound template)."""

    total: int = 0
    total_window: int = 0
    total_7d: int = 0
    active_now: int = 0
    last_activity_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    avg_messages: Optional[float] = Field(
        default=None, description="Mean messages per session inside the window."
    )
    zero_message_share: Optional[float] = Field(
        default=None,
        description="Share of window sessions with no messages (widget opened, nothing typed).",
    )
    daily: List[int] = Field(
        default_factory=list,
        description="Sessions per IST day, aligned with AssistFleetResponse.days.",
    )


class FleetMerchantUsage(BaseModel):
    """Sessions for the merchant across every template it ever used."""

    total: int = 0
    total_window: int = 0
    total_7d: int = 0
    last_activity_at: Optional[datetime] = None


class FleetFlag(BaseModel):
    level: FlagLevel
    code: str
    text: str


class FleetCleanup(BaseModel):
    action: CleanupAction
    reason: str


class FleetTemplate(BaseModel):
    """Summary of one template — enough to classify, never the body."""

    id: str
    name: str
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    agent_type: AgentType
    widget_voice: bool = Field(
        description="Chat agent whose widget may escalate to a WebRTC voice call."
    )
    channels: List[str] = Field(default_factory=list)
    generation: Generation
    matches_reference: bool = False
    prompt_chars: int = 0
    sections: List[str] = Field(default_factory=list)
    shared_block_hash: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    wismo: bool = False
    functions: List[str] = Field(default_factory=list)
    mcp_servers: List[str] = Field(default_factory=list)
    flavors: List[str] = Field(default_factory=list)
    connectors: List[str] = Field(default_factory=list)
    quick_replies: int = 0
    has_greeting: bool = False
    brand_name: Optional[str] = None
    bound_widget_config_id: Optional[str] = None
    bound_merchant_key: Optional[str] = None
    usage: Optional[FleetUsage] = None
    cleanup: Optional[FleetCleanup] = Field(
        default=None,
        description="Set only for orphaned chat templates under the assist resellers.",
    )


class FleetWidget(BaseModel):
    id: str
    active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    appearance_set: bool = False
    max_sessions_per_ip_hour: Optional[int] = None
    max_messages_per_ip_hour: Optional[int] = None
    max_concurrent_per_ip: Optional[int] = None
    max_voice_sessions_per_ip_hour: Optional[int] = None


class FleetMerchantRow(BaseModel):
    exists: bool
    name: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None


class FleetMerchant(BaseModel):
    """One assist agent: a widget_config with everything hanging off it."""

    key: str = Field(description="``reseller_id|merchant_id``.")
    reseller_id: str
    merchant_id: str
    merchant_domain: str = Field(
        description="merchant_id with the standalone-app ``assist-`` prefix removed."
    )
    host_app: HostApp
    platform: Platform
    brand: str
    brand_domain: Optional[str] = None
    origins: List[str] = Field(default_factory=list)
    widget: FleetWidget
    merchant_row: FleetMerchantRow
    template: FleetTemplate
    usage: FleetUsage
    merchant_usage: FleetMerchantUsage
    voice_templates: int = 0
    flags: List[FleetFlag] = Field(default_factory=list)


class FleetVariant(BaseModel):
    """One distinct ``## Operating principles`` block, by content hash."""

    hash: str
    is_reference: bool = False
    template_ids: List[str] = Field(default_factory=list)
    live_count: int = 0


class FleetIssue(BaseModel):
    level: FlagLevel
    title: str
    detail: str
    fix: Optional[str] = None


class FleetReference(BaseModel):
    source: Literal["blueprint", "template", "majority", "none"]
    template_ids: List[str] = Field(default_factory=list)
    hashes: List[str] = Field(default_factory=list)


class FleetTotals(BaseModel):
    merchants: int = 0
    active_7d: int = 0
    sessions_total: int = 0
    sessions_window: int = 0
    sessions_7d: int = 0
    silent: int = 0
    orphans: int = 0
    orphans_deletable: int = 0
    standard: int = 0
    bare: int = 0
    wismo: int = 0
    voice_templates: int = 0
    by_reseller: Dict[str, int] = Field(default_factory=dict)


class AssistFleetResponse(BaseModel):
    """Body of ``GET /admin/assist-fleet``."""

    generated_at: datetime
    window_days: int
    days: List[str] = Field(
        description="IST calendar days covered by every ``daily`` series, oldest first."
    )
    assist_resellers: List[str]
    reference: FleetReference
    merchants: List[FleetMerchant]
    templates: List[FleetTemplate]
    variants: List[FleetVariant]
    issues: List[FleetIssue]
    totals: FleetTotals


__all__ = [
    "AgentType",
    "AssistFleetResponse",
    "CleanupAction",
    "FlagLevel",
    "FleetCleanup",
    "FleetFlag",
    "FleetIssue",
    "FleetMerchant",
    "FleetMerchantRow",
    "FleetMerchantUsage",
    "FleetReference",
    "FleetTemplate",
    "FleetTotals",
    "FleetUsage",
    "FleetVariant",
    "FleetWidget",
    "Generation",
    "HostApp",
    "Platform",
]
