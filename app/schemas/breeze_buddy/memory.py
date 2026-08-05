"""Validated configuration and database records for persistent user memory."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.embeddings import EmbeddingConfig

MemoryBackendName = Literal["pgvector", "supermemory"]
MemoryKeyType = Literal["customer_id", "phone"]
MemoryCategory = Literal["preference", "attribute", "outcome", "context"]


class MemoryEngineConfig(BaseModel):
    """Global policy shared by every template that opts into memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: MemoryBackendName = "pgvector"
    identity_field: str = Field("customer_id", min_length=1)
    phone_field: str = Field("customer_mobile_number", min_length=1)
    phone_default_region: Optional[str] = Field(
        None,
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$",
    )
    allow_phone_fallback: bool = True
    retention_days: int = Field(180, ge=1, le=3650)
    max_facts: int = Field(100, ge=1, le=10_000)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    @field_validator("identity_field", "phone_field", mode="before")
    @classmethod
    def _strip_field_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("phone_default_region", mode="before")
    @classmethod
    def _normalize_region(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        return normalized or None


class UserMemory(BaseModel):
    id: UUID
    reseller_id: str
    merchant_id: str
    customer_key: str
    key_type: MemoryKeyType
    fact: str
    category: Optional[MemoryCategory] = None
    structured: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None
    source_channel: Optional[Literal["voice", "chat"]] = None
    confidence: float = 1.0
    operation_key: Optional[str] = None
    expires_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CustomerIdentity(BaseModel):
    id: UUID
    reseller_id: str
    merchant_id: str
    phone: str
    customer_id: str
    status: Literal["ACTIVE", "CONFLICTED"] = "ACTIVE"
    conflicting_customer_id: Optional[str] = None
    conflicted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class MemoryKey(BaseModel):
    reseller_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    customer_key: str = Field(min_length=1)
    key_type: MemoryKeyType
