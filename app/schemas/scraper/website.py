"""Schemas for provider-neutral website scraping."""

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
