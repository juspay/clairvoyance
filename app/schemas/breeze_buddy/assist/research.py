"""Schemas for website scraping and for the store research stream."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ResearchErrorCode = Literal[
    "UNREADABLE_SITE", "RESEARCH_UNAVAILABLE", "RESEARCH_FAILED"
]
# "finished" when every chosen page was read; "out_of_time" when the budget
# ended the run early and the notes are what was found by then.
ResearchStop = Literal["finished", "out_of_time"]


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


class ResearchNote(BaseModel):
    """One fact, and the page it was read on (a ``note`` event)."""

    field: str
    value: str
    source_url: str


class ResearchDone(BaseModel):
    """Every fact the run kept (the ``done`` event)."""

    notes: List[ResearchNote]
    pages_read: int
    stopped_because: ResearchStop


class ResearchError(BaseModel):
    """Why the run ended without a result (the ``error`` event)."""

    code: ResearchErrorCode
    message: str
    retryable: bool = False
