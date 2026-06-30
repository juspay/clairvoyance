"""Schemas for merchant WhatsApp credential storage."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MerchantWhatsAppCredentialSecrets(BaseModel):
    """Sensitive WhatsApp credential values stored in encrypted credential JSON."""

    access_token: str
    token_type: Optional[str] = "bearer"
    access_token_expires_at: Optional[datetime] = None
    business_id: Optional[str] = None
    waba_id: str
    phone_number_id: str
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None


class MerchantWhatsAppCredentials(BaseModel):
    """Merchant WhatsApp connection status without exposing access tokens."""

    id: str
    credential_id: str
    reseller_id: str
    merchant_id: str
    business_id: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    is_connected: bool = True
    messages_sent_count: int = Field(default=0, ge=0)
    connected_at: Optional[datetime] = None
    last_message_sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MerchantWhatsAppCredentialsWithSecrets(MerchantWhatsAppCredentials):
    """Internal credential model for sending WhatsApp messages."""

    access_token: str
    token_type: Optional[str] = None
    access_token_expires_at: Optional[datetime] = None


class UpsertMerchantWhatsAppCredentials(BaseModel):
    """Input used after Meta Embedded Signup completes in a later PR."""

    reseller_id: str
    merchant_id: str
    access_token: str
    business_id: Optional[str] = None
    waba_id: str
    phone_number_id: str
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    token_type: Optional[str] = "bearer"
    access_token_expires_at: Optional[datetime] = None
