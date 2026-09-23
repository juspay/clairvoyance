"""Request and response shapes for ``POST /assist/brand``."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandLook


class BrandRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(None, max_length=255)
    # Off skips the rendered-page provider and answers from the platform's own
    # brand block plus the logo alone — faster, and useful when a provider is
    # rate limited or a caller only wants the free sources.
    use_provider: bool = True


class BrandColorOut(BaseModel):
    role: str
    hex: str
    # What said so, so a wrong colour is arguable instead of mysterious.
    source: str
    confidence: float = 0.0


class BrandFontOut(BaseModel):
    family: str
    role: str
    source: str = ""


class BrandResponse(BaseModel):
    colors: List[BrandColorOut] = Field(default_factory=list)
    fonts: List[BrandFontOut] = Field(default_factory=list)
    logo_url: Optional[str] = None
    color_scheme: Optional[str] = None
    # Candidates that were set aside — the console offers these when the
    # merchant disagrees with the pick.
    alternates: List[BrandColorOut] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    fetched_at: Optional[datetime] = None

    @classmethod
    def from_look(cls, look: BrandLook) -> "BrandResponse":
        def colors(entries) -> List[BrandColorOut]:
            return [
                BrandColorOut(
                    role=entry.role,
                    hex=entry.hex,
                    source=entry.source,
                    confidence=entry.confidence,
                )
                for entry in entries
            ]

        return cls(
            colors=colors(look.colors),
            fonts=[
                BrandFontOut(family=f.family, role=f.role, source=f.source)
                for f in look.fonts
            ],
            logo_url=look.logo_url,
            color_scheme=look.color_scheme,
            alternates=colors(look.alternates),
            sources=look.sources,
            warnings=look.warnings,
            fetched_at=look.fetched_at,
        )


__all__ = ["BrandColorOut", "BrandFontOut", "BrandRequest", "BrandResponse"]
