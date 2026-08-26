"""Pydantic schemas for merchant connector state and metrics."""

from datetime import date, datetime
from enum import Enum
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


class ConnectorStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


def _validate_connector_name(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+", normalized):
        raise ValueError(
            "connector must contain only lowercase letters, numbers, and underscores"
        )
    return normalized


class Connector(BaseModel):
    id: str
    reseller_id: str
    merchant_id: str
    connector: str
    credential_id: Optional[str] = None
    status: ConnectorStatus
    connected_at: Optional[datetime] = None
    disconnected_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UpsertConnectorConnection(BaseModel):
    """Internal input for an atomic connector credential and state sync."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    connector: str = Field(..., min_length=1, max_length=255)
    credential_value: str = Field(..., min_length=1)
    credential_is_encrypted: bool = False
    credential_description: Optional[str] = Field(None, max_length=500)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("connector")
    @classmethod
    def validate_connector(cls, value: str) -> str:
        return _validate_connector_name(value)


class ConnectorMetric(BaseModel):
    id: str
    connector_id: str
    merchant_id: str
    reseller_id: str
    metric_date: date
    metric_name: str
    value: int = Field(ge=0)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConnectorMetricIncrement(BaseModel):
    connector_id: str
    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: str = Field(..., min_length=1, max_length=255)
    metric_name: str = Field(..., min_length=1, max_length=255)
    increment: int = Field(..., ge=0)
    metric_date: Optional[date] = None


__all__ = [
    "Connector",
    "ConnectorMetric",
    "ConnectorMetricIncrement",
    "ConnectorStatus",
    "UpsertConnectorConnection",
]
