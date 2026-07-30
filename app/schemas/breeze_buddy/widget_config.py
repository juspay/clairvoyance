"""Pydantic models for widget_config rows (migration 030).

widget_config is the per-merchant configuration that powers the
widget-public auth mode (CHAT_MODE.md §14). One row per merchant; an
opaque ``public_widget_key`` is generated server-side and embedded in
the merchant's widget snippet.

public_widget_key is **never** accepted on Create/Update — it's
read-only on the response. Callers that want to rotate it should add
a dedicated rotate endpoint later; this PR keeps the surface minimal.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class WidgetConfigCreate(BaseModel):
    """Body of ``POST /widget-config``."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    template_id: str = Field(
        ...,
        description="UUID of the template the widget will run. Must exist; "
        "FK constraint enforces this.",
    )
    allowed_origins: List[str] = Field(
        default_factory=list,
        description="Exact-match list of allowed Origin / Referer values. "
        "Empty list means deny all (widget cannot be used until populated).",
    )
    max_sessions_per_ip_hour: int = Field(60, ge=0)
    max_messages_per_ip_hour: int = Field(600, ge=0)
    max_concurrent_per_ip: int = Field(4, ge=0)
    max_voice_sessions_per_ip_hour: int = Field(10, ge=0)
    human_assist_enabled: bool = False
    human_assist_platform: str = Field(
        "native",
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="Registered platform adapter used for new handoffs.",
    )
    active: bool = Field(True, description="Inactive rows behave like 404.")


class WidgetConfigUpdate(BaseModel):
    """Body of ``PUT /widget-config/{id}``.

    All fields optional — only the fields supplied are updated.
    ``public_widget_key`` deliberately not present.
    """

    template_id: Optional[str] = None
    allowed_origins: Optional[List[str]] = None
    max_sessions_per_ip_hour: Optional[int] = Field(None, ge=0)
    max_messages_per_ip_hour: Optional[int] = Field(None, ge=0)
    max_concurrent_per_ip: Optional[int] = Field(None, ge=0)
    max_voice_sessions_per_ip_hour: Optional[int] = Field(None, ge=0)
    human_assist_enabled: Optional[bool] = None
    human_assist_platform: Optional[str] = Field(
        None, pattern=r"^[a-z][a-z0-9_]{0,63}$"
    )
    active: Optional[bool] = None


class WidgetConfigResponse(BaseModel):
    """One widget_config row as returned by GET/list/create."""

    id: str
    reseller_id: str
    merchant_id: str
    public_widget_key: str
    template_id: str
    allowed_origins: List[str] = Field(default_factory=list)
    max_sessions_per_ip_hour: int = 60
    max_messages_per_ip_hour: int = 600
    max_concurrent_per_ip: int = 4
    max_voice_sessions_per_ip_hour: int = 10
    human_assist_enabled: bool = False
    human_assist_platform: str = "native"
    active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WidgetConfigListResponse(BaseModel):
    """Body of ``GET /widget-config/list``."""

    widget_configs: List[WidgetConfigResponse]
    total: int
    page: int = 1
    limit: int = 50


class DeleteWidgetConfigResponse(BaseModel):
    """Body of ``DELETE /widget-config/{id}``."""

    status: str = "success"
    message: str
    deleted_id: str


__all__ = [
    "WidgetConfigCreate",
    "WidgetConfigUpdate",
    "WidgetConfigResponse",
    "WidgetConfigListResponse",
    "DeleteWidgetConfigResponse",
]
