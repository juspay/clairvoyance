"""Breeze Buddy connection and configuration schemas."""

from typing import Optional

from pydantic import BaseModel


class BreezeBuddyDailyConnectRequest(BaseModel):
    """Connection request for Breeze Buddy agent via Daily transport"""

    call_sid: Optional[str] = None
