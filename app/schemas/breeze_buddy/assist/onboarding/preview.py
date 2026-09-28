"""Response shape for ``POST /assist/onboarding/preview`` (the request is
``ProbeRequest``)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PreviewResponse(BaseModel):
    """The detected look. Nothing is saved."""

    primary_color: Optional[str] = None
    logo_url: Optional[str] = None
    icon_url: Optional[str] = None


__all__ = ["PreviewResponse"]
