"""Breeze Buddy connection and configuration schemas."""

from pydantic import BaseModel, Field


class BreezeBuddyDailyConnectRequest(BaseModel):
    """Request model for Breeze Buddy Daily transport connection."""

    lead_id: str = Field(
        ..., description="Lead ID from lead_call_tracker table to use for the session"
    )
