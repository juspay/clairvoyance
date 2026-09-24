"""Schemas for provider-neutral website scraping."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WebsiteScrapingRequest(BaseModel):
    reseller_id: str
    merchant_id: Optional[str] = None
    provider: str = Field(
        ...,
        description="Website scraping provider to use.",
    )
    provider_config: Dict[str, Any] = Field(default_factory=dict)
    url: str
    timeout_seconds: int = 18


class WebsiteScrapingResult(BaseModel):
    text: str
    status: str
    url_context_metadata: List[Dict[str, str]]


class WebsiteScrapingResponse(BaseModel):
    success: bool = True
    provider: str
    result: WebsiteScrapingResult
    provider_response: Dict[str, Any]
    error: Optional[str] = None


# ── The researcher's own shapes ──────────────────────────────────────────────
# Kept beside the older single-call scrape rather than replacing it: live
# merchants are onboarded through that one today, and they move when the
# onboarding pipeline moves, not when this endpoint appears.


class SiteResearchRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(None, max_length=255)
    # The vertical's briefing. The engine has no idea what business this is
    # and must not: what a shop's customers need answered and what a clinic's
    # do belong to the vertical, not to the mechanism.
    guidance: Optional[str] = Field(None, max_length=4000)
    # Room to say "be quick" on a retry, or "dig" on a site that hid.
    max_steps: Optional[int] = Field(None, ge=1, le=30)
    max_reads: Optional[int] = Field(None, ge=1, le=200)


class ResearchNote(BaseModel):
    """One fact, and the page that said it."""

    field: str
    value: str
    source_url: str
    # False means it came from the wider web rather than the brand's own site,
    # which is weaker evidence and should be shown as such.
    on_site: bool = True
    noted_at: Optional[datetime] = None


class SiteResearchResponse(BaseModel):
    url: str
    model: str = ""
    summary: str = ""
    notes: List[ResearchNote] = Field(default_factory=list)
    # What it tried, in order. A run that found nothing is only debuggable if
    # you can see the tactics it went through.
    trace: List[str] = Field(default_factory=list)
    steps_used: int = 0
    documents_read: int = 0
    stopped_because: str = ""
    platform: str = ""
    slots: Optional["SiteSlotsOut"] = None
    # The sections rendered as the text a template is built from. Returned
    # so onboarding can build from exactly what the merchant confirmed,
    # rather than reading the site a second time and getting something
    # subtly different from what they just approved.
    context: str = ""
    # The same values keyed by the studio's own field names, flat. This is
    # what onboarding builds from and what the studio later edits, so a
    # merchant sees one set of fields rather than two vocabularies.
    fields: Dict[str, List[str]] = Field(default_factory=dict)


class SlotFieldOut(BaseModel):
    """One field of a section, with where each value was read."""

    key: str
    label: str
    values: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)


class SlotSectionOut(BaseModel):
    key: str
    title: str
    fields: List[SlotFieldOut] = Field(default_factory=list)


class SiteSlotsOut(BaseModel):
    """The sections an assistant gets built from, filled in from the notes."""

    # Which set of sections this is: a shop's, or the neutral one a site
    # nobody recognises gets.
    profile: str
    sections: List[SlotSectionOut] = Field(default_factory=list)
    unplaced: List[str] = Field(default_factory=list)
    filled_count: int = 0
