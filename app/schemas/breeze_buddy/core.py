"""Core domain schemas for Breeze Buddy (leads, outbound numbers, configurations)."""

from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class OutboundNumberStatus(str, Enum):
    """Status of outbound phone numbers"""

    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class CallProvider(str, Enum):
    """Supported telephony providers"""

    TWILIO = "TWILIO"
    EXOTEL = "EXOTEL"
    PLIVO = "PLIVO"


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


class CallDirection(str, Enum):
    """Direction of the call - inbound or outbound"""

    INBOUND = "INBOUND"  # Customer called us
    OUTBOUND = "OUTBOUND"  # We called customer


class PreCheckType(str, Enum):
    """Supported pre-check types"""

    EXTERNAL_API = "external_api"


class PreCheckDefaultAction(str, Enum):
    """What to do when a pre-check API call fails or times out"""

    PROCEED = "proceed"  # Fail-open: allow the call
    SKIP = "skip"  # Fail-closed: block the call


class PreCheckHttpRequest(BaseModel):
    """HTTP request configuration for pre-check API calls.
    Matches HttpRequestConfig structure for compatibility with HttpRequestExecutor."""

    url: str
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[Dict[str, Any]] = None
    auth: Optional[Dict[str, Any]] = None
    query_params: Optional[Dict[str, Any]] = None
    timeout: int = Field(default=10, ge=1, le=30)
    max_retries: int = Field(default=2, ge=1, le=5)


class PreCheckResponseConfig(BaseModel):
    """Configuration for interpreting pre-check API responses.

    Example:
        # Proceed only if blocked=false
        {"response_field": "blocked", "response_field_value": false}

        # Proceed only if status="active"
        {"response_field": "status", "response_field_value": "active"}
    """

    response_field: str = Field(description="JSON field name in response to check")
    response_field_value: Any = Field(
        description="Expected value. Proceed only when field equals this value."
    )


class PreCheckConfig(BaseModel):
    """Configuration for a single pre-check.

    Example:
        {
            "type": "external_api",
            "name": "DND Check",
            "enabled": true,
            "credential_id": "uuid-of-credential",
            "http_request": {
                "url": "{api_base_url}/can-call?phone={customer_mobile_number}",
                "method": "GET",
                "headers": {"token": "{api_token}"},
                "timeout": 5,
                "max_retries": 2
            },
            "response_config": {
                "response_field": "blocked",
                "response_field_value": false
            },
            "default_on_failure": "proceed"
        }
    """

    type: PreCheckType = PreCheckType.EXTERNAL_API
    name: str = Field(description="Human-readable name for logging (e.g., 'DND Check')")
    enabled: bool = True
    credential_id: Optional[str] = Field(
        default=None,
        description="UUID of the credential to use for placeholder resolution",
    )
    http_request: PreCheckHttpRequest
    response_config: PreCheckResponseConfig
    default_on_failure: PreCheckDefaultAction = Field(
        default=PreCheckDefaultAction.PROCEED,
        description="What to do if the pre-check API call fails/times out. 'proceed' = fail-open, 'skip' = fail-closed",
    )


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
    call_direction: CallDirection = CallDirection.OUTBOUND
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
    pre_checks: Optional[List[PreCheckConfig]] = None


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
    pre_checks: Optional[List[PreCheckConfig]] = None


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
    pre_checks: Optional[List[PreCheckConfig]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
