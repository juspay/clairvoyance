"""WhatsApp-specific request and secret schemas.

Connection state is represented by the generic connector schemas.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME = "rsa-oaep-sha256"


class WhatsAppCredentialSecret(BaseModel):
    """Opaque Nautilus-encrypted WhatsApp token payload."""

    encrypted_access_token: str = Field(..., min_length=1)
    token_encryption_scheme: str = Field(WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME)

    @field_validator("token_encryption_scheme")
    @classmethod
    def validate_token_encryption_scheme(cls, value: str) -> str:
        """Only accept the Nautilus-to-Clairvoyance token encryption scheme."""
        if value != WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME:
            raise ValueError(
                "token_encryption_scheme must be "
                f"{WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME}"
            )
        return value


class SyncMerchantWhatsAppConnection(BaseModel):
    """Input required to sync a Nautilus WhatsApp connection into Clairvoyance."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    waba_id: str = Field(..., min_length=1, max_length=255)
    phone_number_id: str = Field(..., min_length=1, max_length=255)
    encrypted_access_token: str = Field(..., min_length=1)
    token_encryption_scheme: str = Field(WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME)
    template_name: Optional[str] = Field(None, max_length=255)
    template_status: Optional[str] = Field(None, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("token_encryption_scheme")
    @classmethod
    def validate_token_encryption_scheme(cls, value: str) -> str:
        """Only accept the Nautilus-to-Clairvoyance token encryption scheme."""
        if value != WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME:
            raise ValueError(
                "token_encryption_scheme must be "
                f"{WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME}"
            )
        return value


__all__ = [
    "SyncMerchantWhatsAppConnection",
    "WhatsAppCredentialSecret",
    "WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME",
]
