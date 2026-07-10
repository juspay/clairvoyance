"""Shared data-source service models."""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from pydantic import BaseModel, Field


class DataSourceUnavailable(Exception):
    """Raised when an external data source cannot be fetched."""


class Capabilities(BaseModel):
    supports_prefetch: bool = False
    is_realtime: bool = False


class DiscoveryDataset(BaseModel):
    name: str
    columns: List[str] = Field(default_factory=list)
    preview_rows: List[Dict[str, Any]] = Field(default_factory=list)


class Discovery(BaseModel):
    datasets: List[DiscoveryDataset] = Field(default_factory=list)


class RawData(BaseModel):
    columns: List[str] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    text: str = ""


class DataSourceAdapter(Protocol):
    source_type: str
    capabilities: Capabilities

    async def discover(self, config: Dict[str, Any]) -> Discovery: ...

    async def fetch_dataset(
        self, config: Dict[str, Any], selector: Dict[str, Any]
    ) -> RawData: ...
