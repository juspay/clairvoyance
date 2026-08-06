"""Pydantic schemas for the credentials table."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator


class CredentialType(str, Enum):
    """Supported credential types"""

    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    BASIC_AUTH = "basic_auth"
    CUSTOM = "custom"


class CreateCredentialRequest(BaseModel):
    """Request to create a credential"""

    reseller_id: Optional[str] = Field(
        default=None,
        description="Reseller ID. NULL for global credentials available to all resellers.",
    )
    merchant_id: Optional[str] = Field(
        default=None,
        description="Merchant ID. NULL makes a reseller credential shared by its merchants.",
    )
    name: str = Field(description="Unique credential name within its scope.")
    credential_type: CredentialType
    value: Dict[str, Any] = Field(
        description="Credential value. For api_key: {'key': '...'}, bearer_token: {'token': '...'}, basic_auth: {'username': '...', 'password': '...'}, custom: any key-value pairs"
    )
    description: Optional[str] = None

    @model_validator(mode="after")
    def validate_scope(self) -> "CreateCredentialRequest":
        if self.merchant_id is not None and self.reseller_id is None:
            raise ValueError("merchant_id requires reseller_id")
        return self


class UpdateCredentialRequest(BaseModel):
    """Request to update a credential"""

    name: Optional[str] = None
    credential_type: Optional[CredentialType] = None
    value: Optional[Dict[str, Any]] = Field(
        default=None,
        description="New credential value. Masked fields ('******') will preserve existing values.",
    )
    description: Optional[str] = None
    is_active: Optional[bool] = None


class Credential(BaseModel):
    """Credential model returned from DB (value is always masked in API responses)"""

    id: str
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    name: str
    credential_type: CredentialType
    value: Optional[Dict[str, Any]] = Field(
        default=None, description="Masked credential value in API responses"
    )
    is_encrypted: bool = False
    description: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
