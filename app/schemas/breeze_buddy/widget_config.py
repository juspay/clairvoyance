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
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class WidgetAppearance(BaseModel):
    """Display-only widget appearance, stored verbatim on the row.

    Every field optional — absent means "widget default". Validation is
    deliberately mechanical (length caps + https on logo URLs); what the
    values look like on screen is the widget SDK's business, not the
    server's. ``launcher_label`` may be an empty string: that is a real
    setting (hide the launcher text), not an absence.
    """

    primary_color: Optional[str] = Field(None, max_length=64)
    header_title: Optional[str] = Field(None, max_length=120)
    launcher_label: Optional[str] = Field(None, max_length=60)
    header_logo_url: Optional[str] = Field(None, max_length=2048)
    launcher_logo_url: Optional[str] = Field(None, max_length=2048)
    offset_x: Optional[str] = Field(None, max_length=16)
    offset_y: Optional[str] = Field(None, max_length=16)
    # Which corner the launcher/panel anchors to (widget SDK vocabulary:
    # "bottom-right" | "bottom-left"); unknown values fall back SDK-side.
    position: Optional[str] = Field(None, max_length=16)
    launcher_height: Optional[str] = Field(None, max_length=16)
    # String, not bool: the SDK parses the literal attribute string
    # ("false" pins the launcher; presence-semantics would break that).
    draggable: Optional[str] = Field(None, max_length=8)
    modes: Optional[str] = Field(None, max_length=32)
    default_mode: Optional[str] = Field(None, max_length=16)
    # Hosted skin (full custom UI in a sandboxed iframe) and hosted
    # theme.css for the default UI. Both are widget-SDK contracts
    # (custom-skin-url / custom-style-url); the server only insists on
    # https. Skins must live on a DEDICATED origin — never the merchant's
    # own page origin (the SDK docs carry the full warning).
    custom_skin_url: Optional[str] = Field(None, max_length=2048)
    custom_style_url: Optional[str] = Field(None, max_length=2048)

    @field_validator(
        "header_logo_url", "launcher_logo_url", "custom_skin_url", "custom_style_url"
    )
    @classmethod
    def _https_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            return None
        parsed = urlsplit(stripped)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("appearance URLs must be HTTPS")
        return stripped


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
    active: bool = Field(True, description="Inactive rows behave like 404.")
    appearance: Optional[WidgetAppearance] = Field(
        None, description="Display-only widget appearance; omitted = defaults."
    )


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
    active: Optional[bool] = None
    appearance: Optional[WidgetAppearance] = Field(
        None,
        description="Replaces the WHOLE appearance object (send the full "
        "form); omitted = leave appearance untouched.",
    )


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
    active: bool = True
    # Served verbatim as stored (a Dict, not WidgetAppearance, so rows
    # written by a newer deploy never fail validation on an older one).
    appearance: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StorefrontWidgetConfigResponse(BaseModel):
    """Body of the public ``GET /widget/storefront-config`` resolve.

    Everything the storefront loader needs to mount the widget, nothing
    more. ``tenant`` is the public widget key — public by design (it is
    embedded in every storefront page). Disabled/unknown/blocked merchants
    never reach this shape; they get 404/403 with no body detail.
    """

    enabled: bool = True
    tenant: str
    merchant_domain: str
    appearance: Dict[str, Any] = Field(default_factory=dict)
    settings_revision: Optional[str] = Field(
        None,
        description="Opaque change fingerprint (row updated_at). The loader "
        "refetches when this differs from its cached copy.",
    )
    cache_ttl_seconds: int = 900


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
    "StorefrontWidgetConfigResponse",
    "WidgetAppearance",
    "WidgetConfigCreate",
    "WidgetConfigUpdate",
    "WidgetConfigResponse",
    "WidgetConfigListResponse",
    "DeleteWidgetConfigResponse",
]
