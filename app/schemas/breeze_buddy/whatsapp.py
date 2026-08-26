"""WhatsApp-specific API schemas backed by generic connector state."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.breeze_buddy.connectors import Connector, ConnectorStatus

WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME = "rsa-oaep-sha256+aes-256-gcm"


class EncryptedWhatsAppAccessToken(BaseModel):
    """Nautilus encrypted Meta access token envelope."""

    encrypted_key: str = Field(..., min_length=1)
    iv: str = Field(..., min_length=1)
    auth_tag: str = Field(..., min_length=1)
    ciphertext: str = Field(..., min_length=1)


class WhatsAppCredentialSecret(BaseModel):
    """Decrypted Meta token payload stored using credential encryption."""

    access_token: str = Field(..., min_length=1)


class SyncMerchantWhatsAppConnection(BaseModel):
    """Input required to sync a Nautilus WhatsApp connection."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    shop_id: Optional[str] = Field(None, min_length=1, max_length=255)
    waba_id: str = Field(..., min_length=1, max_length=255)
    phone_number_id: str = Field(..., min_length=1, max_length=255)
    encrypted_access_token: EncryptedWhatsAppAccessToken
    token_encryption_scheme: str = Field(WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME)
    template_name: Optional[str] = Field(None, max_length=255)
    template_status: Optional[str] = Field(None, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("token_encryption_scheme")
    @classmethod
    def validate_token_encryption_scheme(cls, value: str) -> str:
        if value != WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME:
            raise ValueError(
                "token_encryption_scheme must be "
                f"{WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME}"
            )
        return value


class WhatsAppConnectionSyncResponse(BaseModel):
    """Response returned after syncing a merchant WhatsApp connection."""

    status: ConnectorStatus
    connector: Connector


class WhatsAppConnectionDisconnectRequest(BaseModel):
    """Input required to disconnect a merchant WhatsApp connection."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)


class WhatsAppConnectionDisconnectResponse(BaseModel):
    """Response returned after disconnecting a merchant WhatsApp connection."""

    status: ConnectorStatus = ConnectorStatus.DISCONNECTED
    reseller_id: str
    merchant_id: str


__all__ = [
    "EncryptedWhatsAppAccessToken",
    "SyncMerchantWhatsAppConnection",
    "WhatsAppConnectionDisconnectRequest",
    "WhatsAppConnectionDisconnectResponse",
    "WhatsAppConnectionSyncResponse",
    "WhatsAppCredentialSecret",
    "WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME",
]
