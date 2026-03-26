"""Core domain schemas for Breeze Buddy (leads, outbound numbers, configurations)."""

from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class OutboundNumberStatus(str, Enum):
    """Status of outbound phone numbers"""

    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class InboundBlockAction(str, Enum):
    """Action to take when an inbound call is blocked."""

    REJECT = "REJECT"
    REDIRECT = "REDIRECT"


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
    DAILY_STREAM = "DAILY_STREAM"  # Daily STT/TTS-only (no LLM, client-driven)


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
    reseller_id: str
    template: str
    template_id: Optional[str] = None
    merchant_id: Optional[str] = None
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
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None


class OutboundNumber(BaseModel):
    """Outbound phone number model"""

    id: str
    number: str
    provider: CallProvider
    status: OutboundNumberStatus
    channels: Optional[int] = None
    maximum_channels: Optional[int] = None
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TelephonyConfig(BaseModel):
    """Per-merchant telephony provider overrides.

    When set on a CallExecutionConfig, these values take precedence over the
    global environment-variable defaults.  Any field left as None will fall
    back to the env default.
    """

    applet_app_id: Optional[str] = None


class CreateCallExecutionConfigRequest(BaseModel):
    """Request to create call execution configuration"""

    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    reseller_id: str
    template: str
    merchant_id: Optional[str] = None
    enable_international_call: bool = True
    enable_calling: Optional[bool] = True
    enable_inbound: Optional[bool] = True
    inbound_call_start_time: Optional[time] = None
    inbound_call_end_time: Optional[time] = None
    inbound_call_timezone: Optional[str] = None
    inbound_block_action: Optional[InboundBlockAction] = None
    inbound_redirect_number: Optional[str] = None
    inbound_block_message: Optional[str] = None
    enforce_blacklist: Optional[bool] = True
    rate_limit_enabled: Optional[bool] = False
    rate_limit_max_calls: Optional[int] = None
    rate_limit_window_seconds: Optional[int] = None
    rate_limit_whitelist: Optional[str] = None
    pre_checks: Optional[List[PreCheckConfig]] = None
    telephony_config: Optional[TelephonyConfig] = None

    @model_validator(mode="after")
    def validate_inbound_policy_consistency(self) -> "CreateCallExecutionConfigRequest":
        # Business hours: both times must be provided together
        if bool(self.inbound_call_start_time) != bool(self.inbound_call_end_time):
            raise ValueError(
                "inbound_call_start_time and inbound_call_end_time must both be set or both be empty"
            )
        # REDIRECT action requires a redirect number
        if (
            self.inbound_block_action == InboundBlockAction.REDIRECT
            and not self.inbound_redirect_number
        ):
            raise ValueError(
                "inbound_redirect_number is required when inbound_block_action is REDIRECT"
            )
        # Rate limit enabled requires max_calls
        if self.rate_limit_enabled and not self.rate_limit_max_calls:
            raise ValueError(
                "rate_limit_max_calls is required when rate_limit_enabled is true"
            )
        return self


class UpdateCallExecutionConfigRequest(BaseModel):
    """Request to update call execution configuration"""

    reseller_id: str
    template: str
    merchant_id: Optional[str] = None
    initial_offset: Optional[int] = None
    retry_offset: Optional[int] = None
    call_start_time: Optional[time] = None
    call_end_time: Optional[time] = None
    max_retry: Optional[int] = None
    calling_provider: Optional[CallProvider] = None
    enable_international_call: Optional[bool] = None
    enable_calling: Optional[bool] = None
    enable_inbound: Optional[bool] = None
    inbound_call_start_time: Optional[time] = None
    inbound_call_end_time: Optional[time] = None
    inbound_call_timezone: Optional[str] = None
    inbound_block_action: Optional[InboundBlockAction] = None
    inbound_redirect_number: Optional[str] = None
    inbound_block_message: Optional[str] = None
    enforce_blacklist: Optional[bool] = None
    rate_limit_enabled: Optional[bool] = None
    rate_limit_max_calls: Optional[int] = None
    rate_limit_window_seconds: Optional[int] = None
    rate_limit_whitelist: Optional[str] = None
    pre_checks: Optional[List[PreCheckConfig]] = None
    telephony_config: Optional[TelephonyConfig] = None

    @model_validator(mode="after")
    def validate_inbound_policy_consistency(self) -> "UpdateCallExecutionConfigRequest":
        # Business hours: if either time is explicitly set, both must be provided
        if (self.inbound_call_start_time is None) != (
            self.inbound_call_end_time is None
        ):
            raise ValueError(
                "inbound_call_start_time and inbound_call_end_time must both be set or both be empty"
            )
        # REDIRECT action requires a redirect number
        if (
            self.inbound_block_action == InboundBlockAction.REDIRECT
            and self.inbound_redirect_number is None
        ):
            raise ValueError(
                "inbound_redirect_number is required when inbound_block_action is REDIRECT"
            )
        # Rate limit enabled requires max_calls
        if self.rate_limit_enabled is True and not self.rate_limit_max_calls:
            raise ValueError(
                "rate_limit_max_calls is required when rate_limit_enabled is true"
            )
        return self


class CallExecutionConfig(BaseModel):
    """Call execution configuration model"""

    id: str
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    reseller_id: str
    template: str
    template_id: Optional[str] = None
    merchant_id: Optional[str] = None
    enable_international_call: bool = True
    enable_calling: bool = True
    enable_inbound: bool = True
    inbound_call_start_time: Optional[time] = None
    inbound_call_end_time: Optional[time] = None
    inbound_call_timezone: Optional[str] = None
    inbound_block_action: InboundBlockAction = InboundBlockAction.REJECT
    inbound_redirect_number: Optional[str] = None
    inbound_block_message: Optional[str] = None
    enforce_blacklist: bool = True
    rate_limit_enabled: bool = False
    rate_limit_max_calls: Optional[int] = None
    rate_limit_window_seconds: Optional[int] = None
    rate_limit_whitelist: Optional[str] = None
    pre_checks: Optional[List[PreCheckConfig]] = None
    telephony_config: Optional[TelephonyConfig] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BlacklistedNumber(BaseModel):
    """Blacklisted phone number model"""

    id: str
    phone_number: str
    reseller_id: Optional[str] = None
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateBlacklistNumberRequest(BaseModel):
    """Request to add a phone number to the blacklist"""

    phone_number: str = Field(min_length=4, max_length=20)
    reseller_id: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=500)
