"""Core domain schemas for Breeze Buddy (leads, outbound numbers, configurations)."""

from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel


class OutboundNumberStatus(str, Enum):
    """Status of outbound phone numbers"""

    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class CallProvider(str, Enum):
    """Supported telephony providers"""

    TWILIO = "TWILIO"
    EXOTEL = "EXOTEL"


class LeadCallStatus(str, Enum):
    """Status of lead call execution"""

    BACKLOG = "BACKLOG"
    PROCESSING = "PROCESSING"
    FINISHED = "FINISHED"
    RETRY = "RETRY"


class ExecutionMode(str, Enum):
    """Execution mode for lead calls - separates production from test calls"""

    TELEPHONY = "TELEPHONY"  # Production telephony calls
    TELEPHONY_TEST = "TELEPHONY_TEST"  # Test telephony calls
    DAILY = "DAILY"  # Production Daily (web) calls
    DAILY_TEST = "DAILY_TEST"  # Test Daily (web) calls


class LeadCallTracker(BaseModel):
    """Lead call tracking model"""

    id: str
    outbound_number_id: Optional[str] = None
    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    request_id: Optional[str] = None
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = None
    payload: Optional[Dict[str, Any]] = None
    metaData: Optional[Dict[str, Any]] = None
    recording_url: Optional[str] = None
    status: LeadCallStatus = LeadCallStatus.BACKLOG
    outcome: Optional[str] = None
    call_id: Optional[str] = None
    call_initiated_time: Optional[datetime] = None
    call_end_time: Optional[datetime] = None
    cost: Optional[float] = None
    is_locked: bool = False
    langfuse_scores: Optional[Dict[str, Any]] = None
    execution_mode: ExecutionMode = ExecutionMode.TELEPHONY
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateOutboundNumberRequest(BaseModel):
    """Request to create a new outbound number"""

    number: str
    provider: CallProvider
    status: OutboundNumberStatus = OutboundNumberStatus.AVAILABLE
    maximum_channels: Optional[int] = None
    merchant_id: Optional[str] = None
    shop_identifier: Optional[str] = None


class OutboundNumber(BaseModel):
    """Outbound phone number model"""

    id: str
    number: str
    provider: CallProvider
    status: OutboundNumberStatus
    channels: Optional[int] = None
    maximum_channels: Optional[int] = None
    merchant_id: Optional[str] = None
    shop_identifier: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateCallExecutionConfigRequest(BaseModel):
    """Request to create call execution configuration"""

    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    enable_international_call: bool = True
    enable_calling: Optional[bool] = True


class UpdateCallExecutionConfigRequest(BaseModel):
    """Request to update call execution configuration"""

    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    initial_offset: Optional[int] = None
    retry_offset: Optional[int] = None
    call_start_time: Optional[time] = None
    call_end_time: Optional[time] = None
    max_retry: Optional[int] = None
    calling_provider: Optional[CallProvider] = None
    enable_international_call: Optional[bool] = None
    enable_calling: Optional[bool] = None


class CallExecutionConfig(BaseModel):
    """Call execution configuration model"""

    id: str
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    enable_international_call: bool = True
    enable_calling: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
