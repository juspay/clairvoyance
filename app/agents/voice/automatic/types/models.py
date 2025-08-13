from dataclasses import dataclass
from enum import Enum
from typing import Union, List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, Json

class TTSProvider(str, Enum):
    ELEVENLABS = "ELEVENLABS"
    GOOGLE = "GOOGLE"

class VoiceName(str, Enum):
    RHEA = "RHEA"
    MIA = "MIA"
    BRET = "BRET"

class Mode(str, Enum):
    TEST = "TEST"
    LIVE = "LIVE"


@dataclass
class ApiSuccess:
    """Represents a successful API response."""
    data: str


@dataclass
class ApiFailure:
    """Represents a failed API response."""
    error: dict


# A union type to represent either outcome
GeniusApiResponse = Union[ApiSuccess, ApiFailure]

# --- MCP-Compliant Pydantic Models ---
class ToolInputSchema(BaseModel):
    type: str = "object"
    properties: Dict[str, Any]
    required: Optional[List[str]] = None

class MCPTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: ToolInputSchema = Field(..., alias="inputSchema")

class ToolsListResult(BaseModel):
    tools: List[MCPTool]

class ToolCallContent(BaseModel):
    type: str
    text: Union[Json[Any], str]

class ToolCallResult(BaseModel):
    content: List[ToolCallContent]

class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None

class JSONRPCResponse(BaseModel):
    jsonrpc: str
    id: int
    result: Optional[Union[ToolsListResult, ToolCallResult]] = None
    error: Optional[JSONRPCError] = None


# --- Field Value Discovery Types ---

# Supported dimensions for field value discovery
CardinalityDimension = Literal[
    "actual_order_status",
    "actual_payment_status", 
    "auth_type",
    "bank",
    "card_brand",
    "card_type",
    "emi",
    "emi_tenure",
    "emi_type",
    "entire_payment_flow",
    "gateway",
    "industry",
    "is_business_retry",
    "is_cvv_less_txn",
    "is_offer_txn",
    "is_requeued_order",
    "is_retargeted_order",
    "is_retried_order",
    "is_technical_retry",
    "is_token_bin",
    "is_tokenized",
    "issuer_token_reference",
    "mandate_feature",
    "order_source_object",
    "order_source_object_id",
    "order_status",
    "order_type",
    "payment_gateway",
    "payment_instrument_group",
    "payment_method_subtype",
    "payment_method_type",
    "payment_status",
    "platform",
    "prev_order_status",
    "prev_txn_status",
    "previous_order_status",
    "previous_txn_status",
    "status_sync_source",
    "stored_card_vault_provider",
    "ticket_size",
    "token_repeat",
    "tokenization_consent",
    "tokenization_consent_ui_presented",
    "tokenization_eligibility",
    "tokenized_flow",
    "txn_conflict",
    "txn_flow_type",
    "txn_latency_enum",
    "txn_object_type",
    "txn_source_object",
    "txn_type",
    "unified_response_category",
    "user_opt_in",
    "using_stored_card",
    "using_token",
    "is_upicc",
    # Relevant aliases for cardinality dimensions
    "payment_provider",  # -> payment_gateway
    "card_network",  # -> card_brand
    "card_provider",  # -> card_brand
    "is_stored_card_transaction", # -> token_repeat
    "transaction_via_saved_card", # -> token_repeat
]


class DimensionLookupRequest(BaseModel):
    """Request for field-value discovery for a single dimension."""
    dimension: CardinalityDimension
    queries: List[str] = Field(default_factory=list)
    max_results: Optional[int] = None


class DimensionLookupResult(BaseModel):
    """Result for a single dimension lookup."""
    dimension: CardinalityDimension
    results: List[List[str]]
    unsupported_message: Optional[str] = None


class FieldLookupBatchResponse(BaseModel):
    """Batch response for field value discovery."""
    results: List[DimensionLookupResult] = Field(default_factory=list)
    error: Optional[str] = None


# --- Q API Types ---

from pydantic import BaseModel, RootModel, Field, ConfigDict, model_validator


#################################
#            Metrics            #
#################################

MetricEnum = Literal[
    # Original metric names
    "total_amount",
    "success_volume",
    "success_rate",
    "avg_ticket_size",
    "conflict_txn_rate",
    "average_latency",
    "order_with_transactions",
    "order_with_transactions_gmv",
    # Additional system metric names (targets of aliases)
    "token_repeat",
    "saved_orders_volume",
    "saved_orders_volume_gateway",
    "saved_orders_amount",
    "saved_orders_amount_gateway",
    # Aliased metric names
    "revenue",  # -> total_amount
    "sales",  # -> total_amount
    "gmv",  # -> total_amount
    "count_of_orders_saved_due_to_silent_retry", # -> saved_orders_volume
    "count_of_orders_saved_due_to_health_based_routing", # -> saved_orders_volume_gateway
    "success_gmv_of_orders_saved_due_to_silent_retry", # -> saved_orders_amount
    "success_gmv_of_orders_saved_due_to_health_based_routing", # -> saved_orders_amount_gateway
]
Metric = Union[MetricEnum, List[MetricEnum]]

#################################
#            Dimensions          #
#################################

class Granularity(BaseModel):
    unit: Literal["second", "minute", "hour", "day", "week", "month"]
    duration: int = Field(..., ge=1)


class DimensionObject(BaseModel):
    granularity: Granularity
    intervalCol: Literal["order_created_at"]
    timeZone: Literal["Asia/Kolkata"]


DimensionString = Literal[
    # Original dimension names
    "actual_order_status",
    "actual_payment_status",
    "allowed_requeue",
    "auth_type",
    "bank",
    "bank_name",
    "business_region",
    "card_bin",
    "card_brand",
    "card_exp_month",
    "card_exp_year",
    "card_issuer_country",
    "card_last_four_digits",
    "card_sub_type",
    "card_type",
    "consent_page",
    "currency",
    "emi",
    "emi_bank",
    "emi_tenure",
    "emi_type",
    "entire_payment_flow",
    "error_message",
    "gateway_reference_id",
    "industry",
    "is_business_retry",
    "is_cvv_less_txn",
    "is_gateway_switched",
    "is_notification_retried",
    "is_offer_txn",
    "is_requeued_order",
    "is_retargeted_order",
    "is_retried_order",
    "is_technical_retry",
    "is_token_bin",
    "is_tokenized",
    "issuer_token_reference",
    "issuer_tokenization_consent_failure_reason",
    "juspay_bank_code",
    "juspay_error_message",
    "juspay_response_code",
    "juspay_response_message",
    "lob",
    "mandate_feature",
    "mandate_execute_retried",
    "mandate_frequency",
    "mandate_source_object",
    "mandate_status",
    "merchant_id",
    "merchant_name",
    "notification_status",
    "ord_currency",
    "order_source_object",
    "order_source_object_id",
    "order_status",
    "order_type",
    "order_created_at",
    "original_card_isin",
    "os",
    "payment_flow",
    "payment_gateway",
    "payment_instrument_group",
    "payment_link_channels",
    "payment_link_sent",
    "payment_method_subtype",
    "payment_method_type",
    "payment_status",
    "platform",
    "prev_gateway_resp_code",
    "prev_gateway_resp_message",
    "prev_order_status",
    "prev_txn_status",
    "previous_gateway_resp_code",
    "previous_gateway_resp_message",
    "previous_order_status",
    "previous_txn_status",
    "priority_logic_tag",
    "requeue_count",
    "resp_code",
    "resp_message",
    "status_sync_source",
    "stored_card_vault_provider",
    "ticket_size",
    "token_reference",
    "token_repeat",
    "tokenization_consent",
    "tokenization_consent_failure_reason",
    "tokenization_consent_ui_presented",
    "tokenization_eligibility",
    "tokenized_flow",
    "txn_conflict",
    "txn_flow_type",
    "txn_latency_enum",
    "txn_object_type",
    "txn_source_object",
    "txn_type",
    "udf1",
    "udf10",
    "udf2",
    "udf3",
    "udf4",
    "udf5",
    "udf6",
    "udf7",
    "udf8",
    "udf9",
    "unified_response_category",
    "use_merchant_proxy",
    "user_opt_in",
    "using_stored_card",
    "using_token",
    "is_upicc",
    # Aliased dimension names
    "payment_provider",  # -> payment_gateway
    "gateway",  # -> payment_gateway
    "card_network",  # -> card_brand
    "card_provider",  # -> card_brand
    "is_stored_card_transaction", # -> token_repeat
    "transaction_via_saved_card", # -> token_repeat
]

#################################
#            Interval            #
#################################

class Interval(BaseModel):
    """Time interval for queries with start and end times"""
    start: str  # format: "%Y-%m-%dT%H:%M:%SZ"
    end: str  # format: "%Y-%m-%dT%H:%M:%SZ"

#################################
#            Filters            #
#################################

MetricCondition = Literal[
    "Greater",
    "GreaterThanEqual",
    "LessThanEqual",
    "Less",
]

class MetricFilter(BaseModel):
    """Filter for metrics using having conditions."""
    metric: MetricEnum
    condition: MetricCondition
    value: Union[int, float]
    
    @model_validator(mode="after")
    def _validate_value_type(self) -> "MetricFilter":
        """Ensure percentage metrics have float values between 0-100."""
        percentage_metrics = {"success_rate", "conflict_txn_rate"}
        if self.metric in percentage_metrics:
            if not (0 <= self.value <= 100):
                raise ValueError(f"{self.metric} must be between 0 and 100")
        return self

FilterFieldDimensionEnum = DimensionString

FilterCondition = Literal[
    "In",
    "NotIn",
    "Greater",
    "GreaterThanEqual",
    "LessThanEqual",
    "Less",
    "HasAny",
    "HasAll",
]

class ValObject(BaseModel):
    """Special helper for Top-N queries"""
    limit: int = Field(ge=1)
    sortedOn: object

class Clause(BaseModel):
    """Single predicate applied to a dimension."""
    field: FilterFieldDimensionEnum
    condition: FilterCondition
    val: Union[
        str, bool, float, None,
        List[Union[str, bool, None]],
        ValObject,
    ]

class FlatFilter(BaseModel):
    """Flat representation of the boolean filter tree."""
    clauses: List[Clause] = Field(..., min_items=1, max_items=10)
    logic: str

    @model_validator(mode="after")
    def _check_logic_indices(self) -> "FlatFilter":
        """Check that logic only references valid indices."""
        import re
        if self.logic:
            max_idx = len(self.clauses) - 1
            for idx in map(int, re.findall(r"\d+", self.logic)):
                if idx > max_idx:
                    raise ValueError(f"logic references non-existent clause #{idx}")
        return self

Filter = FlatFilter | None

#################################
#         Response Type         #
#################################

class QApiSuccessRow(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
        },
    )

class QApiSuccessResponse(RootModel[List[QApiSuccessRow]]):
    pass

class QApiErrorResponse(BaseModel):
    error: str
    payload_attempted: Dict[str, Any]

QApiResponse = Union[QApiSuccessResponse, QApiErrorResponse]

#################################
#        QApiPayload Type       #
#################################

class QApiPayload(BaseModel):
    """Pydantic model for the Q API payload"""
    domain: Literal["kvorders"] = "kvorders"
    metric: Metric
    interval: Interval
    filters: Optional[Filter] = None
    dimensions: List[Union[DimensionString, DimensionObject]] = Field(default_factory=list)
    sortedOn: Optional[object] = None
    metric_filters: Optional[List[MetricFilter]] = None

# --- End of Models ---
