"""Feature flags schemas."""

from typing import Any, Dict

from pydantic import BaseModel, Field


class FeatureFlagUpdate(BaseModel):
    flags: Dict[str, Any] = Field(..., description="Flag key-value pairs to update")


class FeatureFlagResponse(BaseModel):
    flags: Dict[str, Any]
    total_count: int


class FeatureFlagUpdateResponse(BaseModel):
    status: str
    message: str
    updated_flags: list[str]
    total_flags: int


class FeatureFlagDeleteResponse(BaseModel):
    status: str
    message: str
    remaining_flags: int
