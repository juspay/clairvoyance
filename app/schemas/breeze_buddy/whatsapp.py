"""Schemas for WhatsApp Embedded Signup onboarding."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppConnectionStatus(str, Enum):
    """Merchant WhatsApp connection state."""

    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"


class MetaEmbeddedSignupSessionInfo(BaseModel):
    """Asset IDs returned by Meta's WA_EMBEDDED_SIGNUP message event."""

    model_config = ConfigDict(extra="allow")

    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    business_id: Optional[str] = None
    waba_ids: Optional[List[str]] = None
    ad_account_ids: Optional[List[str]] = None
    page_ids: Optional[List[str]] = None
    dataset_ids: Optional[List[str]] = None
    catalog_ids: Optional[List[str]] = None
    instagram_account_ids: Optional[List[str]] = None


class MetaEmbeddedSignupEvent(BaseModel):
    """Raw WA_EMBEDDED_SIGNUP event posted by Loom."""

    model_config = ConfigDict(extra="allow")

    data: MetaEmbeddedSignupSessionInfo = Field(
        default_factory=MetaEmbeddedSignupSessionInfo
    )
    type: Optional[str] = None
    event: Optional[str] = None


class MetaEmbeddedSignupCompleteRequest(BaseModel):
    """Complete merchant WhatsApp onboarding after Meta returns a code."""

    merchant_id: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)
    signup_event: MetaEmbeddedSignupEvent
    subscribe_webhooks: bool = True
    register_phone_number: bool = False
    phone_number_pin: Optional[str] = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="Required when register_phone_number=true.",
    )


class WhatsAppRegisterPhoneRequest(BaseModel):
    """Register an already connected merchant phone number with Cloud API."""

    pin: str = Field(..., pattern=r"^\d{6}$")


class WhatsAppSendPaymentLinkRequest(BaseModel):
    """Send the approved payment-link WhatsApp template to a customer."""

    recipient_phone: str = Field(..., min_length=6, max_length=32)
    customer_name: str = Field(..., min_length=1, max_length=128)
    order_reference: str = Field(..., min_length=1, max_length=128)
    payment_link: str = Field(..., min_length=1, max_length=2048)


class WhatsAppConnectionResponse(BaseModel):
    """Masked WhatsApp connection metadata returned to Loom."""

    merchant_id: str
    reseller_id: Optional[str] = None
    business_token_credential_id: Optional[str] = None
    status: WhatsAppConnectionStatus
    connected: bool = False
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    meta_business_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    app_id: Optional[str] = None
    config_id: Optional[str] = None
    graph_api_version: Optional[str] = None
    webhook_subscribed: bool = False
    phone_registered: bool = False
    token_type: Optional[str] = None
    token_expires_at: Optional[datetime] = None
    scope: Optional[str] = None
    payment_link_template_id: Optional[str] = None
    payment_link_template_name: Optional[str] = None
    payment_link_template_language: Optional[str] = None
    payment_link_template_category: Optional[str] = None
    payment_link_template_status: Optional[str] = None
    payment_link_template_created_at: Optional[datetime] = None
    payment_link_template_approved_at: Optional[datetime] = None
    last_template_error: Optional[str] = None
    messages_attempted_count: int = 0
    messages_success_count: int = 0
    messages_failed_count: int = 0
    last_message_attempted_at: Optional[datetime] = None
    last_message_success_at: Optional[datetime] = None
    last_message_failed_at: Optional[datetime] = None
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    last_onboarding_event: Optional[str] = None
    connected_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WhatsAppEmbeddedSignupConfigResponse(BaseModel):
    """Frontend configuration needed to launch Meta Embedded Signup."""

    merchant_id: str
    app_id: str
    config_id: str
    graph_api_version: str
    status: WhatsAppConnectionStatus
    connected: bool
    connection: Optional[WhatsAppConnectionResponse] = None


class WhatsAppTokenPayload(BaseModel):
    """Internal decrypted WhatsApp token payload."""

    access_token: str


class MetaTokenExchangeResult(BaseModel):
    """Meta OAuth token exchange response normalized for storage."""

    access_token: str
    token_type: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class MetaTemplateCreateResult(BaseModel):
    """Meta message template creation response normalized for storage."""

    id: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class MetaMessageSendResult(BaseModel):
    """Meta WhatsApp message send response normalized for callers."""

    message_id: Optional[str] = None
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class WhatsAppSendPaymentLinkResponse(BaseModel):
    """Response for a merchant WhatsApp payment-link send attempt."""

    message_id: Optional[str] = None
    connection: WhatsAppConnectionResponse
