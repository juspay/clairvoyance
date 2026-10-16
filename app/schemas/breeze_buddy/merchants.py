"""Response schemas for merchant endpoints."""

import re
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    field_validator,
    model_validator,
)


class MerchantCreate(BaseModel):
    """Create a new merchant entity (business entity).

    - Admin: can set reseller_id to assign merchant to a reseller (or leave null)
    - Reseller: reseller_id is auto-set to their own user ID
    """

    merchant_id: str = Field(
        ...,
        min_length=3,
        max_length=100,
        description="Unique business identifier (alphanumeric, underscore, hyphen only)",
    )
    name: Optional[str] = Field(
        None, max_length=255, description="Optional display name"
    )
    description: Optional[str] = Field(None, description="Optional description")
    is_active: Optional[bool] = Field(True, description="Active status (default: true)")
    reseller_id: Optional[str] = Field(
        None,
        description="Reseller user ID who owns this merchant. "
        "Admin-only field; auto-set for resellers.",
    )
    issue_token: bool = Field(
        False,
        description="If true, mint a per-merchant S2S token, store it on the "
        "merchant row, and return it (once) in the response. Used e.g. as the "
        "webhook HMAC secret. Requires a reseller_id.",
    )
    token_lifetime_days: int = Field(
        default=3650,
        ge=1,
        le=365000,
        description="Lifetime of the issued token in days (only when issue_token).",
    )


class MerchantUpdate(BaseModel):
    """Update a merchant entity. merchant_id cannot be changed."""

    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    reseller_id: Optional[str] = Field(
        None, description="Update reseller assignment (admin only)"
    )


class MerchantResponse(BaseModel):
    """Merchant entity response.

    ``token`` / ``token_expires_at`` are populated only on create when
    ``issue_token`` was requested; they are null on list/get/update.
    """

    merchant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    reseller_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    token: Optional[str] = Field(
        None,
        description="Per-merchant S2S token, returned once when issue_token=true. "
        "Store it now — it is not retrievable later.",
    )
    token_expires_at: Optional[str] = Field(
        None, description="ISO timestamp when the issued token expires"
    )


class MerchantListResponse(BaseModel):
    """Response for listing merchant entities."""

    merchants: List[MerchantResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1


# --------------------------------------------------------------------------
# Per-customer call limits (ADR 0025 stage 1) — the merchant's rule, enforced
# at the dial by the dispatch worker (services/call_limiter.py).
# --------------------------------------------------------------------------

# A rolling window longer than a week is not a call-frequency rule any more.
CALL_LIMIT_MAX_WINDOW_HOURS = 168
# Stage 1 takes exactly one rule; stage 2 lifts this for "a day AND a week".
CALL_LIMIT_MAX_RULES = 1


class CallLimit(BaseModel):
    """At most ``max_calls`` dials to one customer in any rolling
    ``window_hours`` — rolling, not calendar: a merchant-chosen window has no
    midnight to reset at.

    Strict ints: ``"3"`` and ``true`` are refused rather than coerced — a
    limit read wrong in the permission-adjacent direction calls someone the
    merchant said to stop calling.
    """

    model_config = ConfigDict(extra="forbid")

    max_calls: StrictInt = Field(..., ge=1, description="Dials allowed in the window")
    window_hours: StrictInt = Field(
        ...,
        ge=1,
        le=CALL_LIMIT_MAX_WINDOW_HOURS,
        description="Rolling window length in hours (1-168)",
    )


class CallLimitsUpdate(BaseModel):
    """``PUT /merchant/{merchant_id}/call-limits`` body.

    ``call_limits`` is REQUIRED so clearing is explicit: ``null`` or ``[]``
    removes the rule (stored as NULL — one form for "no rule").
    """

    model_config = ConfigDict(extra="forbid")

    call_limits: Optional[List[CallLimit]] = Field(
        ...,
        max_length=CALL_LIMIT_MAX_RULES,
        description="The merchant's per-customer call rules; null or [] for none",
    )

    @field_validator("call_limits")
    @classmethod
    def _empty_is_none(cls, value: Optional[List[CallLimit]]):
        return value or None


class CallLimitsResponse(BaseModel):
    """The merchant's per-customer call rules (``null`` = no rule)."""

    merchant_id: str
    call_limits: Optional[List[CallLimit]] = None


# --------------------------------------------------------------------------
# Analytics field config — which key in the merchant's JSON fills which
# analytics column. Written by the merchant, read by the ClickHouse views via
# the PeerDB mirror of merchants. The future page config (charts, queries,
# tabs) is a separate column, analytics_page_config: read by the console, not
# mirrored.
# --------------------------------------------------------------------------

# shared slots: one meaning for every merchant, so reports can sum across them
ANALYTICS_SHARED_SLOTS: Dict[str, str] = {
    "amount": "number",
    "category": "text",
    "reason": "text",
    "product_name": "text",
    "language": "text",
    "region": "text",
    "order_id": "text",
}
ANALYTICS_CUSTOM_TEXT_SLOTS = 10
ANALYTICS_CUSTOM_NUMBER_SLOTS = 5
# slot -> column type; the slot decides, a rule carries no type
ANALYTICS_SLOT_KINDS: Dict[str, str] = {
    **ANALYTICS_SHARED_SLOTS,
    **{f"custom_text_{i}": "text" for i in range(1, ANALYTICS_CUSTOM_TEXT_SLOTS + 1)},
    **{
        f"custom_num_{i}": "number" for i in range(1, ANALYTICS_CUSTOM_NUMBER_SLOTS + 1)
    },
}
ANALYTICS_FIELD_SCOPE_ALL = "*"
# reloaded into a ClickHouse dictionary every minute: keep it small
ANALYTICS_FIELD_CONFIG_MAX_RULES = 200
# payload.<key>[.<key>] (what the merchant sent) or learned.<key> (meta_data.outcome)
_ANALYTICS_PATH_PATTERN = re.compile(r"^(payload|learned)(\.[A-Za-z0-9_-]+)+$")
# a template id (calls), a topic such as order.created (events), or *
_ANALYTICS_SCOPE_PATTERN = re.compile(r"^(\*|[A-Za-z0-9_.:-]{1,128})$")


class AnalyticsFieldRule(BaseModel):
    """Where one slot is read from, and what dashboards call it."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        max_length=200,
        description="payload.<key> or learned.<key>; dots walk into nested objects",
    )
    label: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Shown by dashboards for this slot",
    )

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        if not _ANALYTICS_PATH_PATTERN.fullmatch(value):
            raise ValueError(
                "path must be payload.<key> or learned.<key>, keys of letters, "
                "digits, _ and -"
            )
        return value


class AnalyticsTable(str, Enum):
    """Analytics tables a merchant can map keys into; one view each."""

    CALLS = "calls"  # analytics.calls, from lead_call_tracker; scope = template id
    EVENTS = "events"  # analytics.events, from crm_event_raw; scope = topic


AnalyticsFieldScopes = Dict[str, Dict[str, AnalyticsFieldRule]]


class AnalyticsFieldConfig(RootModel[Dict[AnalyticsTable, AnalyticsFieldScopes]]):
    """``{table: {scope: {slot: rule}}}``. Scope: a template id (calls), a
    topic (events) or ``*``; the views try the row's scope, then ``*``."""

    @field_validator("root")
    @classmethod
    def _known_scopes_and_slots(
        cls, value: Dict[AnalyticsTable, AnalyticsFieldScopes]
    ) -> Dict[AnalyticsTable, AnalyticsFieldScopes]:
        for scopes in value.values():
            for scope, slots in scopes.items():
                if not _ANALYTICS_SCOPE_PATTERN.fullmatch(scope):
                    raise ValueError(
                        f"scope {scope!r} must be a template id, a topic or *"
                    )
                for slot in slots:
                    if slot not in ANALYTICS_SLOT_KINDS:
                        raise ValueError(
                            f"unknown slot {slot!r}; use a shared key "
                            f"({', '.join(ANALYTICS_SHARED_SLOTS)}), custom_text_1.."
                            f"{ANALYTICS_CUSTOM_TEXT_SLOTS} or custom_num_1.."
                            f"{ANALYTICS_CUSTOM_NUMBER_SLOTS}"
                        )
        return value

    @model_validator(mode="after")
    def _bounded(self) -> "AnalyticsFieldConfig":
        if self.rule_count() > ANALYTICS_FIELD_CONFIG_MAX_RULES:
            raise ValueError(
                f"at most {ANALYTICS_FIELD_CONFIG_MAX_RULES} rules per merchant"
            )
        return self

    def rule_count(self) -> int:
        return sum(
            len(slots) for scopes in self.root.values() for slots in scopes.values()
        )

    def is_empty(self) -> bool:
        return self.rule_count() == 0


class AnalyticsConfigUpdate(BaseModel):
    """``PUT /merchant/{merchant_id}/analytics-config`` body. A key sent is
    replaced whole (``null`` or empty clears it, stored as NULL); a key left
    out is untouched; at least one key. ``analytics_page_config`` joins later."""

    model_config = ConfigDict(extra="forbid")

    analytics_field_config: Optional[AnalyticsFieldConfig] = Field(
        default=None, description="The merchant's mapping; null or empty for none"
    )

    @field_validator("analytics_field_config")
    @classmethod
    def _empty_is_none(cls, value: Optional[AnalyticsFieldConfig]):
        return None if value is None or value.is_empty() else value

    @model_validator(mode="after")
    def _something_to_set(self) -> "AnalyticsConfigUpdate":
        if not self.model_fields_set:
            raise ValueError("send at least one config key: analytics_field_config")
        return self


class AnalyticsConfigResponse(BaseModel):
    """The merchant's analytics configs (``null`` = none)."""

    merchant_id: str
    analytics_field_config: Optional[AnalyticsFieldConfig] = None
