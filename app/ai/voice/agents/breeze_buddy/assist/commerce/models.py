"""The commerce vertical's research and slot shapes.

The engine's ``SiteResearch`` is what any website yields; a shop adds its
catalogue. ``CommerceSlots`` is the only merchant-specific content a
commerce template carries (plan §3.2).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.assist.engine.models import SiteResearch


class ResearchProduct(BaseModel):
    title: str
    url: Optional[str] = None
    product_type: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    options: List[Dict[str, Any]] = Field(default_factory=list)
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    currency: Optional[str] = None
    image_url: Optional[str] = None
    available: Optional[bool] = None
    source: Optional[str] = None


class ResearchCollection(BaseModel):
    title: str
    url: Optional[str] = None
    image_url: Optional[str] = None
    product_count: Optional[int] = None


class CommerceResearch(SiteResearch):
    categories: List[str] = Field(default_factory=list)
    products: List[ResearchProduct] = Field(default_factory=list)
    collections: List[ResearchCollection] = Field(default_factory=list)


class CommerceSlots(BaseModel):
    brand_block: str
    sizing_section: str
    link_label: str
    link_url: str
    link_noun: Optional[str] = None
    guided: Dict[str, str] = Field(default_factory=dict)
    greeting: str
    quick_replies: List[Dict[str, Any]] = Field(default_factory=list)
    tiles: List[Dict[str, Any]] = Field(default_factory=list)
    trusted_urls: List[str] = Field(default_factory=list)
    checkout_url: Optional[str] = None
    example_product: Optional[str] = None
    sources: Dict[str, str] = Field(default_factory=dict)


__all__ = ["CommerceResearch", "CommerceSlots", "ResearchCollection", "ResearchProduct"]
