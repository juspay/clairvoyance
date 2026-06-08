"""Pydantic models for persistent user memory."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel


class UserMemory(BaseModel):
    id: UUID
    reseller_id: str
    merchant_id: str
    customer_key: str
    key_type: str
    fact: str
    category: Optional[str] = None
    structured: Dict[str, Any] = {}
    embedding: Optional[List[float]] = None
    source_channel: Optional[str] = None
    confidence: float = 1.0
    superseded_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CustomerIdentity(BaseModel):
    id: UUID
    reseller_id: str
    merchant_id: str
    phone: str
    customer_id: str
    created_at: datetime
    updated_at: datetime


class MemoryKey(BaseModel):
    reseller_id: str
    merchant_id: str
    customer_key: str
    key_type: str
