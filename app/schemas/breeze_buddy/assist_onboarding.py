"""Request and SSE payload contracts for Buddy Assist onboarding."""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from app.schemas.breeze_buddy.widget_config import WidgetAppearance


def _public_https_url(value: str, *, origin_only: bool) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("must not be empty")
    normalized = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.port is not None and parsed.port != 443)
    ):
        raise ValueError("must be a public HTTPS URL")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        raise ValueError("must use a public host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("must use a public host")

    if origin_only and (parsed.path not in ("", "/") or parsed.query):
        raise ValueError("must contain only scheme and host")

    netloc = hostname
    if parsed.port and parsed.port != 443:
        netloc = f"{hostname}:{parsed.port}"
    path = "" if origin_only else (parsed.path.rstrip("/") or "")
    return urlunsplit(
        ("https", netloc, path, parsed.query if not origin_only else "", "")
    )


class AssistOnboardingStreamRequest(BaseModel):
    """Request body for the Buddy Assist onboarding stream."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    merchant_name: str = Field(..., min_length=1, max_length=255)
    website_url: str = Field(..., max_length=2048)
    is_shopify: bool
    allowed_origins: List[str] = Field(..., max_length=20)
    provider: Literal["google"] = "google"
    bot_brand_name: Optional[str] = Field(None, min_length=1, max_length=255)
    is_active: bool = True

    @field_validator("reseller_id", "merchant_id", "merchant_name", "bot_brand_name")
    @classmethod
    def _strip_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("website_url")
    @classmethod
    def _validate_website_url(cls, value: str) -> str:
        return _public_https_url(value, origin_only=False)

    @field_validator("allowed_origins")
    @classmethod
    def _validate_allowed_origins(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            if len(value) > 2048:
                raise ValueError("allowed origin is too long")
            origin = _public_https_url(value, origin_only=True)
            if origin not in normalized:
                normalized.append(origin)
        return normalized


class AssistOnboardRequest(BaseModel):
    """Body of the non-streaming ``POST /assist/onboard`` (S2S).

    The bare-metal install path: called by the Shopify-app layer at app
    install. Creates only the un-personalized default — merchant row,
    blueprint-derived template, widget config — and NEVER scrapes the
    storefront; personalization is a later, dashboard-triggered action.

    Tenancy is derived server-side from ``host_app`` + ``merchant_domain``
    (see assist/commerce/tenancy.py): the breeze-buddy host app lands on
    reseller ``BB_SHOPIFY`` with the plain domain; the standalone
    buddy-assist app on ``BB_ASSIST`` with an ``assist-`` prefixed
    merchant id. Callers cannot pick a namespace directly.
    """

    merchant_domain: str = Field(..., min_length=4, max_length=253)
    host_app: Literal["buddy-assist", "breeze-buddy"] = "buddy-assist"
    merchant_name: Optional[str] = Field(None, min_length=1, max_length=255)
    bot_brand_name: Optional[str] = Field(None, min_length=1, max_length=255)
    allowed_origins: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="Extra allowed origins (e.g. the merchant's primary domain). "
        "https://<merchant_domain> is always included.",
    )
    appearance: Optional[WidgetAppearance] = None
    is_active: bool = True

    @field_validator("merchant_name", "bot_brand_name")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("allowed_origins")
    @classmethod
    def _validate_extra_origins(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            if len(value) > 2048:
                raise ValueError("allowed origin is too long")
            origin = _public_https_url(value, origin_only=True)
            if origin not in normalized:
                normalized.append(origin)
        return normalized


class AssistOnboardResponse(BaseModel):
    """Body of ``POST /assist/onboard``."""

    success: Literal[True] = True
    operation: Literal["created", "updated", "recovered"]
    merchant_created: bool
    template_id: str
    template_name: str
    widget_config: Dict[str, Any]


class AssistOnboardingProgress(BaseModel):
    step: str
    status: Literal["running", "done"]
    details: Dict[str, Any] = Field(default_factory=dict)


class AssistOnboardingError(BaseModel):
    success: Literal[False] = False
    step: str
    code: str
    message: str
    retryable: bool = False


class AssistOnboardingCompletion(BaseModel):
    success: Literal[True] = True
    operation: Literal["created", "updated", "recovered"]
    template_id: str
    template_name: str
    widget_config: Dict[str, Any]
    personalization: Dict[str, Any]


__all__ = [
    "AssistOnboardRequest",
    "AssistOnboardResponse",
    "AssistOnboardingCompletion",
    "AssistOnboardingError",
    "AssistOnboardingProgress",
    "AssistOnboardingStreamRequest",
]
