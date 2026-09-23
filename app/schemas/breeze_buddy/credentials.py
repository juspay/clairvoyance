"""Pydantic schemas for the credentials table."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


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
        description="Merchant ID for a merchant-scoped credential (requires "
        "reseller_id). NULL = reseller-wide. Resolution: merchant row, else "
        "reseller row, else global.",
    )
    name: str = Field(
        description="Unique name used as the placeholder key (e.g., 'shopify_api_key')"
    )
    credential_type: CredentialType
    value: Dict[str, Any] = Field(
        description="Credential value. For api_key: {'key': '...'}, bearer_token: {'token': '...'}, basic_auth: {'username': '...', 'password': '...'}, custom: any key-value pairs"
    )
    description: Optional[str] = None
    provider: Optional[str] = Field(
        default=None,
        description="The provider ACCOUNT this row holds, for a template's "
        "llm/stt/tts `credential_id` (e.g. 'elevenlabs', 'azure_openai', "
        "'deepgram'). The value then carries what that provider needs "
        "(`api_key`; Azure also `endpoint`; Vertex `credentials_json` + "
        "`project_id`). NULL = a placeholder credential, as before.",
    )


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
    provider: Optional[str] = None


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
    provider: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
