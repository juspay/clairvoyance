"""Request and reply of ``POST /assist/template/create``."""

from __future__ import annotations

import re
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.schemas.breeze_buddy.assist.onboarding import (
    AssistOnboardingStreamRequest,
    OnboardingPlatform,
    _public_https_url,
)
from app.schemas.breeze_buddy.assist.onboarding.research import AssistResearchNote
from app.schemas.breeze_buddy.widget_config import WidgetAppearance


class AssistTemplateRequest(BaseModel):
    """Body of ``POST /assist/template/create``: make the assistant, switched off.

    ``notes`` are the research findings the console showed the merchant;
    they are sorted into the vertical's fields here, by the server's rules.
    ``appearance`` is the starting look (the colour and logo the preview
    found). The widget allows the store's own origin.
    """

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    merchant_name: str = Field(..., min_length=1, max_length=255)
    website_url: str = Field(..., max_length=2048)
    platform: OnboardingPlatform
    notes: List[AssistResearchNote] = Field(default_factory=list, max_length=500)
    appearance: Optional[WidgetAppearance] = None

    @field_validator("reseller_id", "merchant_id", "merchant_name")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("website_url")
    @classmethod
    def _validate_website_url(cls, value: str) -> str:
        url = _public_https_url(value, origin_only=False)
        # A host name only: "{openai_api_key}.x.com" would become a placeholder
        # once the host is written into the prompt.
        if not re.fullmatch(r"[a-z0-9.-]+", urlsplit(url).netloc):
            raise ValueError("must be a public HTTPS URL")
        return url

    def as_onboarding_request(self) -> AssistOnboardingStreamRequest:
        """The same merchant as an onboarding request, switched off, so the
        onboarding builders serve both paths."""
        host = urlsplit(self.website_url).netloc
        # A store answers on both its bare and its www address (one usually
        # redirects to the other), so the widget allows both.
        twin = host.removeprefix("www.") if host.startswith("www.") else f"www.{host}"
        return AssistOnboardingStreamRequest(
            reseller_id=self.reseller_id,
            merchant_id=self.merchant_id,
            merchant_name=self.merchant_name,
            website_url=self.website_url,
            platform=self.platform,
            allowed_origins=[f"https://{host}", f"https://{twin}"],
            is_active=False,
        )


class AssistTemplateResponse(BaseModel):
    """Body of ``POST /assist/template/create``: the new assistant, for the console to
    open its Build page."""

    success: Literal[True] = True
    template_id: str


__all__ = ["AssistTemplateRequest", "AssistTemplateResponse"]
