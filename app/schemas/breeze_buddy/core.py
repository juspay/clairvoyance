"""Core domain schemas for Breeze Buddy (leads, telephony numbers, configurations)."""

from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig


class TelephonyNumberStatus(str, Enum):
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
    HOLD_TRANSFER = "HOLD_TRANSFER"  # Outbound leg of a hold & consultative transfer


class CallDirection(str, Enum):
    """Direction of the call - inbound or outbound"""

    INBOUND = "INBOUND"  # Customer called us
    OUTBOUND = "OUTBOUND"  # We called customer


# The ONLY sanctioned template_id-less lead rows, both inbound artifacts
# (everything else must be id-linked — resolution is id-only, PRs #888/#889):
#  - IVR_OPTIONS_TEMPLATE: multi-template number, caller still in the digit
#    menu; updated with the real template on selection, stays a placeholder
#    forever if they hang up mid-menu (census 2026-07-14: ~23k such rows/30d
#    on nammayatri — expected, exclude from cutover template_id audits).
#  - UNKNOWN_TEMPLATE: blocked call on a number with no template mapping.
IVR_OPTIONS_TEMPLATE = "IVR-OPTIONS"
UNKNOWN_TEMPLATE = "unknown"
TEMPLATELESS_PLACEHOLDER_TEMPLATES = frozenset({IVR_OPTIONS_TEMPLATE, UNKNOWN_TEMPLATE})


class PreCheckType(str, Enum):
    """Supported pre-check types"""

    EXTERNAL_API = "external_api"
    INTERNAL_FUNCTION = "internal_function"


class PreCheckFailureAction(str, Enum):
    """What to do with the lead when a pre-check blocks the call.

    ``abort`` is the historical (and default) behaviour. ``defer`` exists for
    transient blocks — a cooldown window that has not elapsed yet, a quota that
    resets later — where killing the lead outright is the wrong answer.
    """

    ABORT = "abort"  # FINISHED / PRECHECK_FAILED, terminal
    DEFER = "defer"  # stay BACKLOG, push next_attempt_at and retry later


class PreCheckDefaultAction(str, Enum):
    """What to do when a pre-check API call fails or times out"""

    PROCEED = "proceed"  # Fail-open: allow the call
    SKIP = "skip"  # Fail-closed: block the call


class PreCheckMatchType(str, Enum):
    """How to compare the response field value against response_field_value.

    The result is always "should the call PROCEED?".
    """

    EQUALS = "equals"  # proceed when field == value (default, back-compatible)
    NOT_EQUALS = "not_equals"  # proceed when field != value
    CONTAINS = "contains"  # proceed when value is present in the field (list or string)
    NOT_CONTAINS = "not_contains"  # proceed when value is ABSENT from the field
    GT = "gt"  # proceed when field > value (numeric)
    LT = "lt"  # proceed when field < value (numeric)


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

        # Proceed only if the order is NOT tagged "no-call"
        # (tags returned as a list; "contains"/"not_contains" are list-aware)
        {"response_field": "tags", "match_type": "not_contains",
         "response_field_value": "no-call"}
    """

    response_field: str = Field(description="JSON field name in response to check")
    response_field_value: Any = Field(
        description="Comparison value. For 'equals'/'not_equals' the field is "
        "compared directly; for 'contains'/'not_contains' this is the needle "
        "looked up inside the field (list membership or substring); for "
        "'gt'/'lt' both sides are compared numerically."
    )
    match_type: PreCheckMatchType = Field(
        default=PreCheckMatchType.EQUALS,
        description="How to compare the field against response_field_value. "
        "Defaults to 'equals' (backward-compatible).",
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

    Omitting ``response_config`` makes the pre-check *fetch-only*: it still
    runs and still exports, but never blocks the call.

    Set ``mcp`` instead of ``http_request`` to call an MCP tool:

        {
            "name": "catalog_lookup",
            "mcp": {"name": "shopify", "url": "...", "auth": {"type": "none"}},
            "mcp_tool": "search_catalog",
            "mcp_arguments": {"catalog": {"query": "{items}"}}
        }

    Set ``type: "internal_function"`` to call an in-repo Python function from
    ``managers/pre_check_functions.PRE_CHECK_FUNCTIONS`` instead of making a
    network hop. The function returns a plain bool and is passed the resolved
    ``function_args``, so ``response_config`` and ``export_to_payload`` do not
    apply:

        {
            "type": "internal_function",
            "name": "Contact cooldown",
            "function": "recent_contact_cooldown",
            "function_args": {"merchant_id": "*", "window_hours": 24},
            "on_failure_action": "defer",
            "defer_seconds": 3600,
            "max_defers": 10,
            "default_on_failure": "proceed"
        }

    ``on_failure_action`` decides what a *block* does to the lead: ``abort``
    (default, terminal) or ``defer`` (retry in ``defer_seconds``, up to
    ``max_defers`` times before falling through to abort).
    """

    type: PreCheckType = PreCheckType.EXTERNAL_API
    name: str = Field(description="Human-readable name for logging (e.g., 'DND Check')")
    enabled: bool = True
    credential_id: Optional[str] = Field(
        default=None,
        description="UUID of the credential to use for placeholder resolution",
    )
    http_request: Optional[PreCheckHttpRequest] = Field(
        default=None,
        description="The HTTP request to run. Unset when using 'mcp'.",
    )
    mcp: Optional[McpServerConfig] = Field(
        default=None,
        description="MCP server to call instead of http_request. Same model as "
        "configurations.mcp.servers[*]; 'enabled' is not consulted.",
    )
    mcp_tool: Optional[str] = Field(
        default=None, description="Tool to call on 'mcp'. Required with 'mcp'."
    )
    mcp_arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for mcp_tool",
    )
    response_config: Optional[PreCheckResponseConfig] = Field(
        default=None,
        description="Go/no-go rule. Omit for a fetch-only pre-check that never "
        "blocks the call.",
    )
    export_to_payload: Optional[Dict[str, str]] = Field(
        default=None,
        description="payload keys projected out of the "
        "response and merged into the lead payload before the dial.",
    )
    default_on_failure: PreCheckDefaultAction = Field(
        default=PreCheckDefaultAction.PROCEED,
        description="What to do if the pre-check API call fails/times out. 'proceed' = fail-open, 'skip' = fail-closed",
    )
    function: Optional[str] = Field(
        default=None,
        description="Key into PRE_CHECK_FUNCTIONS. Required when "
        "type='internal_function', forbidden otherwise.",
    )
    function_args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments passed to 'function'. Values support the same "
        "{placeholder} syntax as http_request, resolved from credentials, "
        "template secrets and the lead payload.",
    )
    on_failure_action: PreCheckFailureAction = Field(
        default=PreCheckFailureAction.ABORT,
        description="What happens to the lead when this pre-check blocks the "
        "call. 'abort' = FINISHED/PRECHECK_FAILED (default), 'defer' = stay "
        "BACKLOG and retry after defer_seconds.",
    )
    defer_seconds: int = Field(
        default=3600,
        ge=30,
        le=86400,
        description="How far out to push next_attempt_at. Only read when "
        "on_failure_action='defer'.",
    )
    max_defers: int = Field(
        default=10,
        ge=1,
        le=100,
        description="How many times a lead may be deferred by pre-checks "
        "before falling through to abort. Only read when "
        "on_failure_action='defer'.",
    )

    @model_validator(mode="after")
    def _validate_type_shape(self) -> "PreCheckConfig":
        """Keep the two pre-check shapes from bleeding into each other.

        This is the only validation layer for pre-checks — the configurations
        router passes the list straight through to the DB — so a typo here has
        to fail loudly at write time rather than silently fail-open at dispatch.
        """
        if self.type == PreCheckType.INTERNAL_FUNCTION:
            if not self.function:
                raise ValueError(
                    "pre-check type 'internal_function' requires 'function' "
                    "(a key in PRE_CHECK_FUNCTIONS)"
                )
            if self.http_request is not None or self.mcp is not None:
                raise ValueError(
                    "pre-check type 'internal_function' cannot also set "
                    "'http_request' or 'mcp'"
                )
        elif self.function is not None:
            raise ValueError(
                f"'function' is only valid with type 'internal_function', "
                f"not '{self.type.value}'"
            )
        return self


class LeadCallTracker(BaseModel):
    """Lead call tracking model"""

    id: str
    telephony_number_id: Optional[str] = None
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
    # CRM identity stamp (migration 050, A15) — written once by the
    # created-lead tap; later mirrors PASS it through, they never resolve.
    customer_id: Optional[str] = None
    # The workflow run that placed this call (migration 059, ADR 0010) —
    # stamped by the walker after insert; the finished tap mirrors it so
    # the run can hear its own call's outcome (rollout phase 18).
    enrollment_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateTelephonyNumberRequest(BaseModel):
    """Request to provision a telephony number.

    Ownership shapes:
      - merchant_id set          → merchant-owned (reseller_id auto-filled
                                   from the merchant's umbrella when omitted)
      - reseller_id only         → umbrella-owned
      - neither + shared_pool    → shared platform pool (the legacy
                                   NULL/NULL fallback pool the dispatcher
                                   scans when a template pins no number)
      - neither, no shared_pool  → rejected (400) so ownership is always an
                                   explicit choice
    """

    number: str
    provider: CallProvider
    status: TelephonyNumberStatus = TelephonyNumberStatus.AVAILABLE
    maximum_channels: Optional[int] = None
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    shared_pool: bool = False


class UpdateTelephonyNumberRequest(BaseModel):
    """Partial update for a telephony number (admin).

    None = leave unchanged. clear_ownership=True nulls reseller_id AND
    merchant_id (returns the number to the shared pool) and wins over any
    ids passed alongside it.
    """

    status: Optional[TelephonyNumberStatus] = None
    maximum_channels: Optional[int] = None
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    clear_ownership: bool = False


class TelephonyNumber(BaseModel):
    """Telephony number model (outbound caller IDs and inbound DIDs)"""

    id: str
    number: str
    provider: CallProvider
    status: TelephonyNumberStatus
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
    """Request to create call execution configuration.

    The config is owned by a template: ``template_id`` is required and the
    scope (reseller_id, merchant_id) plus the template name are derived from
    the template row — clients never send them (legacy scope fields in the
    body are ignored).
    """

    template_id: str
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    enable_international_call: bool = True
    enable_calling: Optional[bool] = True
    enable_inbound: Optional[bool] = True
    inbound_call_start_time: Optional[time] = None
    inbound_call_end_time: Optional[time] = None
    inbound_call_timezone: Optional[str] = None
    inbound_block_action: Optional[InboundBlockAction] = None
    inbound_redirect_number: Optional[str] = None
    inbound_block_message: Optional[str] = None
    inbound_outside_hours_message: Optional[str] = None
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
    """Request to update call execution configuration.

    The config is addressed by its own id (path parameter); scope and
    template name are immutable display data and are not part of the
    request. ``template_id`` is optional: when the stored config already
    has one it must match (the link is immutable); when the stored config
    predates the link it may be supplied to adopt the config onto its
    template. Legacy scope fields in the body are ignored.
    """

    template_id: Optional[str] = None
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
    inbound_outside_hours_message: Optional[str] = None
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
    inbound_outside_hours_message: Optional[str] = None
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
