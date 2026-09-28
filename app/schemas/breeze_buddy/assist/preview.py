"""Response shape for ``POST /assist/preview`` (the request is ``ProbeRequest``)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandColor


class PreviewResponse(BaseModel):
    """The detected look, each colour with the source that said so. Nothing
    is saved; ``warnings`` say which sources were skipped or set aside."""

    colors: List[BrandColor] = Field(default_factory=list)
    logo_url: Optional[str] = None
    icon_url: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


__all__ = ["PreviewResponse"]
