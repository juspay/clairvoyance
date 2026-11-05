"""Schemas for Buddy Copilot scope resolution."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

DEFAULT_COPILOT_TIMEZONE = "Asia/Kolkata"
COPILOT_SCOPE_METADATA_KEY = "copilot"


class CopilotCapability(str, Enum):
    """Read-only capabilities enabled in the Phase 1 scope contract."""

    ANALYTICS_SUMMARY = "get_analytics_summary"
    QUERY_CONVERSATIONS = "query_conversations"
    CONVERSATION_DETAIL = "get_conversation_detail"


class CopilotDateRangeSource(str, Enum):
    """Where the resolved date range came from."""

    REQUEST = "request"
    DEFAULT = "default"


class CopilotRequestedDateRange(BaseModel):
    """Optional dashboard-provided date range for Copilot data reads."""

    date_from: date
    date_to: date

    @field_validator("date_to")
    @classmethod
    def validate_order(cls, value: date, info) -> date:
        date_from = info.data.get("date_from")
        if date_from and value < date_from:
            raise ValueError("date_to must be on or after date_from")
        return value


class CopilotScopeRequest(BaseModel):
    """Dashboard-requested data context for Buddy Copilot.

    The browser may request a merchant/template/date scope, but the resolver
    remains authoritative and returns the immutable CopilotScope.
    """

    data_merchant_id: Optional[str] = Field(
        default=None,
        description="Selected merchant whose analytics/conversations are queried.",
    )
    data_template_id: Optional[str] = Field(
        default=None,
        description="Optional selected agent/template under data_merchant_id.",
    )
    timezone: str = DEFAULT_COPILOT_TIMEZONE
    date_range: Optional[CopilotRequestedDateRange] = None

    @field_validator("data_merchant_id", "data_template_id")
    @classmethod
    def strip_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("data_template_id")
    @classmethod
    def validate_data_template_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("data_template_id must be a valid UUID") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("timezone must not be empty")
        return stripped


class CopilotActorScope(BaseModel):
    """Authenticated actor snapshot used for audit and downstream policy."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    username: str
    role: str
    permissions: tuple[str, ...] = Field(default_factory=tuple)
    reseller_ids: tuple[str, ...] = Field(default_factory=tuple)
    merchant_ids: tuple[str, ...] = Field(default_factory=tuple)


class CopilotDataScope(BaseModel):
    """Selected merchant data scope for analytics and conversation reads."""

    model_config = ConfigDict(frozen=True)

    data_merchant_id: str
    data_template_id: Optional[str] = None


class CopilotDateWindow(BaseModel):
    """Timezone-aware date window normalized exactly once by the resolver."""

    model_config = ConfigDict(frozen=True)

    timezone: str
    date_from: date
    date_to: date
    source: CopilotDateRangeSource

    @computed_field
    @property
    def label(self) -> str:
        """Human-readable description derived from the resolved window."""
        if self.source == CopilotDateRangeSource.DEFAULT:
            return "Last 7 days"
        return f"{self.date_from.isoformat()} to {self.date_to.isoformat()}"


class CopilotScope(BaseModel):
    """Immutable data contract injected into Buddy Copilot sessions and tools."""

    model_config = ConfigDict(frozen=True)

    actor: CopilotActorScope
    data: CopilotDataScope
    date_window: CopilotDateWindow
    capabilities: tuple[CopilotCapability, ...]

    def session_metadata(self) -> Dict[str, Dict[str, object]]:
        """Return semantic scope metadata for the normal Assist chat session."""
        return {COPILOT_SCOPE_METADATA_KEY: self.model_dump(mode="json")}


class CopilotResolvedScopeResponse(BaseModel):
    """Serializable envelope returned by later Copilot session endpoints."""

    scope: CopilotScope
    warnings: List[str] = Field(default_factory=list)
