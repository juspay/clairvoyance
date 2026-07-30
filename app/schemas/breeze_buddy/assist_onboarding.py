"""Schemas for Buddy Assist SSE onboarding."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AssistOnboardingStreamRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    reseller_id: str
    merchant_id: str
    shop_url: str = Field(alias="shopUrl")
    is_shopify: bool = Field(alias="isShopify")
    template_id: Optional[str] = Field(default=None, alias="templateId")
    widget_config_id: Optional[str] = Field(default=None, alias="widgetConfigId")
    allowed_origins: List[str] = Field(default_factory=list, alias="allowedOrigins")
    brand_name: Optional[str] = Field(default=None, alias="brandName")
    header_title: Optional[str] = Field(default=None, alias="headerTitle")
    is_active: bool = True


class AssistOnboardingCompletePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool = True
    template_id: str = Field(alias="templateId")
    template_name: str = Field(alias="templateName")
    widget_config_id: str = Field(alias="widgetConfigId")
    public_widget_key: str = Field(alias="publicWidgetKey")
    allowed_origins: List[str] = Field(alias="allowedOrigins")
    max_sessions_per_ip_hour: int
    max_messages_per_ip_hour: int
    max_concurrent_per_ip: int
    max_voice_sessions_per_ip_hour: int
    active: bool
    personalized: bool
    personalization_status: str = Field(alias="personalizationStatus")
    personalization_failure_reason: Optional[str] = Field(
        default=None, alias="personalizationFailureReason"
    )
    brand_profile: Optional[Dict[str, Any]] = Field(default=None, alias="brandProfile")
    brand_profile_source: str = Field(alias="brandProfileSource")
    prompt_hash: str = Field(alias="promptHash")
