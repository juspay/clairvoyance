"""Validated contracts for persistent user memory."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.embeddings import EmbeddingConfig

MemoryBackendName = Literal["pgvector", "supermemory"]
MemoryKeyType = Literal["customer_id", "phone"]
MemoryCategory = Literal["preference", "attribute", "outcome", "context"]


class MemoryEngineConfig(BaseModel):
    """Validated global policy shared by every opted-in template."""

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


class MemoryIdentity(BaseModel):
    """Canonical tenant/customer scope plus identifiers observed this turn."""

    reseller_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    customer_key: str = Field(min_length=1)
    key_type: MemoryKeyType
    phone: Optional[str] = None
    explicit_customer_id: Optional[str] = None

    @property
    def scope_digest(self) -> str:
        canonical = json.dumps(
            [self.reseller_id, self.merchant_id, self.customer_key],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def scope_tag(self) -> str:
        """Opaque hosted-provider tag with no raw tenant/customer identifiers."""
        return f"bbmem_v1_{self.scope_digest[:48]}"


class MemoryFact(BaseModel):
    id: Optional[str] = None
    fact: str = Field(min_length=1, max_length=10_000)
    category: Optional[MemoryCategory] = None
    structured: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    source_channel: Optional[Literal["voice", "chat"]] = None
    updated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class _MemoryOperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(min_length=1, max_length=10_000)
    category: Optional[MemoryCategory] = None
    structured: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class MemoryAddOperation(_MemoryOperationBase):
    op: Literal["ADD"]


class MemoryUpdateOperation(_MemoryOperationBase):
    op: Literal["UPDATE"]
    supersedes_fact: str = Field(min_length=1, max_length=10_000)


class MemoryDeleteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["DELETE"]
    fact: str = Field(min_length=1, max_length=10_000)


MemoryOperation = Annotated[
    Union[MemoryAddOperation, MemoryUpdateOperation, MemoryDeleteOperation],
    Field(discriminator="op"),
]


class MemoryExtractionJob(BaseModel):
    """Durable queue payload; transcript content remains in the source database."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["voice_lead", "chat_session"]
    record_id: str = Field(min_length=1)
    identity: MemoryIdentity
    source_channel: Literal["voice", "chat"]
    backend: MemoryBackendName
    retention_days: int = Field(180, ge=1, le=3650)
    max_facts: int = Field(100, ge=1, le=10_000)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    idempotency_key: str = Field(min_length=1, max_length=512)
    attempt: int = Field(0, ge=0)
    enqueued_at: datetime
    last_error: Optional[str] = None


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

    def as_fact(self) -> MemoryFact:
        return MemoryFact(
            id=str(self.id),
            fact=self.fact,
            category=self.category,
            structured=self.structured,
            confidence=self.confidence,
            source_channel=self.source_channel,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
        )


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
