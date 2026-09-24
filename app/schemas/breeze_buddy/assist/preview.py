"""Request and response shapes for ``POST /assist/preview``."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandColor


class PreviewRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(None, max_length=255)


class PreviewResponse(BaseModel):
    """The detected look, each colour with the source that said so. Nothing
    is saved; ``appearance`` is what onboarding would start the widget with."""

    colors: List[BrandColor] = Field(default_factory=list)
    logo_url: Optional[str] = None
    alternates: List[BrandColor] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    appearance: Dict[str, Any] = Field(default_factory=dict)


__all__ = ["PreviewRequest", "PreviewResponse"]
