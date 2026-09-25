"""Schemas for website scraping and for store research."""

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


class SiteResearchRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(None, max_length=255)
