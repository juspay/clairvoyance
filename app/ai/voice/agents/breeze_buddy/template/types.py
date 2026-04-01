"""
Pydantic models for the dynamic workflow engine.
"""

import ipaddress
import re
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class ActionType(str, Enum):
    TTS_SAY = "tts_say"
    END_CONVERSATION = "end_conversation"
    FUNCTION = "function"


class VadConfig(BaseModel):
    """VAD configuration for template or node-level customization."""

    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="VAD confidence threshold (0.0-1.0)"
    )
    start_secs: Optional[float] = Field(
        None, ge=0.0, description="Seconds of speech before starting transcription"
    )
    stop_secs: Optional[float] = Field(
        None, ge=0.0, description="Seconds of silence before stopping transcription"
    )
    min_volume: Optional[float] = Field(
        None, ge=0.0, description="Minimum volume threshold for VAD"
    )


class NoiseFilterType(str, Enum):
    """Types of noise filters available for audio input processing."""

    AIC = "aic"  # ai-coustics noise enhancement filter


class NoiseFilterConfig(BaseModel):
    """Configuration for audio input noise filtering."""

    enable: bool = Field(False, description="Whether to enable the noise filter")
    type: NoiseFilterType = Field(
        NoiseFilterType.AIC, description="Type of noise filter to use"
    )


class CartesiaVoiceConfiguration(BaseModel):
    """Cartesia voice configuration parameters for template-level customization.

    Allows per-template override of Cartesia TTS parameters.
    Values specified here take precedence over global Redis defaults.

    TODO: Add validation for emotion strings against known Cartesia emotions
    TODO: Add validation for language codes
    """

    voice_id: Optional[str] = Field(None, description="Cartesia voice ID (e.g., UUID)")
    volume: Optional[float] = Field(
        None,
        ge=0.5,
        le=2.0,
        description="Volume multiplier (Cartesia range: 0.5-2.0)",
    )
    speed: Optional[float] = Field(
        None,
        ge=0.6,
        le=1.5,
        description="Speed multiplier (Cartesia range: 0.6-1.5)",
    )
    emotion: Optional[str] = Field(
        None, description="Voice emotion (e.g., 'neutral', 'excited', 'happy')"
    )
    language: Optional[str] = Field(
        None, description="TTS language code (e.g., 'en', 'hi')"
    )


class ElevenLabsVoiceConfiguration(BaseModel):
    """ElevenLabs voice configuration parameters for template-level customization.

    Allows per-template override of ElevenLabs TTS parameters.
    Values specified here take precedence over global Redis defaults.
    """

    voice_id: Optional[str] = Field(None, description="ElevenLabs voice ID")
    model_id: Optional[str] = Field(
        None, description="ElevenLabs model ID (e.g., 'eleven_flash_v2_5')"
    )
    speed: Optional[float] = Field(
        None,
        ge=0.7,
        le=1.2,
        description="Speed multiplier (ElevenLabs range: 0.7-1.2, where 1.0 is default)",
    )
    language: Optional[str] = Field(
        None, description="TTS language code (e.g., 'en', 'hi')"
    )


class TTSVoiceName(str, Enum):
    RHEA = "rhea"
    SARA = "sara"
    MIRA = "mira"


class TTSProvider(str, Enum):
    """Supported TTS providers for intelligent selection."""

    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"


class TTSSelectionConfig(BaseModel):
    """Configuration for LLM-based TTS provider selection from payload.

    When enabled, uses Gemini to analyze the lead payload and select
    the optimal TTS provider based on rules defined in the prompt.

    Example:
        {
            "enabled": true,
            "prompt": "Based on the customer's address and region, decide the TTS provider.
                       For Hindi and English speaking regions (North India), use 'elevenlabs'.
                       For South Indian regions or if unsure, use 'cartesia'.",
            "providers": ["elevenlabs", "cartesia"]
        }
    """

    enabled: bool = False
    prompt: str = Field(
        ...,
        description="Prompt template for Gemini to decide which TTS provider to use. "
        "The payload will be appended to this prompt automatically.",
    )
    providers: List[TTSProvider] = Field(
        ...,
        min_length=1,
        description="Allowed TTS providers the LLM can choose from.",
    )


class BackgroundSoundFile(str, Enum):
    """Enum for available background sound files"""

    OFFICE_AMBIENCE = "office-ambience"


class KeywordMatchType(str, Enum):
    """Match strategy for keyword filtering."""

    EXACT = "exact"  # Transcription must equal the keyword (case-insensitive, trimmed)
    INCLUDES = "includes"  # Transcription must contain the keyword (case-insensitive)


class KeywordFilterConfig(BaseModel):
    """Configuration for filtering out specific transcriptions during bot activity.

    When the bot is actively speaking (TTS) or the LLM is processing a response,
    user speech matching these keywords is silently dropped — it is neither forwarded
    to the LLM nor treated as an interruption.

    Example:
        {
            "enabled": true,
            "keywords": ["hello", "yes", "okay"],
            "match_type": "exact"
        }
    """

    enabled: bool = False
    keywords: List[str] = Field(
        default_factory=list,
        description="Keywords to filter out while bot is active.",
    )
    match_type: KeywordMatchType = Field(
        KeywordMatchType.EXACT,
        description="Whether the transcription must exactly equal or just contain a keyword.",
    )


class UserIdleHandlingConfig(BaseModel):
    """Configuration for user idle detection and handling."""

    enabled: bool = False
    timeout: float = Field(
        5.0,
        ge=0.0,
        description="User idle detection timeout in seconds. After this period of silence, the system will prompt the user.",
    )
    idle_message: str = Field(
        "The user has been quiet for a while. Ask if they are still there and re-engage them in the conversation.",
        description="System message to prompt LLM when user is idle.",
    )
    max_retries: int = Field(
        3,
        ge=1,
        description="Maximum number of idle timeout events before ending the call with 'busy' outcome. The user receives at most max_retries prompts. The call ends on the (max_retries+1)th event.",
    )


# Private/internal hostname patterns for SSRF protection
_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)


def _is_private_or_reserved_ip(ip_str: str) -> bool:
    """Check if an IP address string is private, loopback, or reserved.

    Args:
        ip_str: IP address as string (IPv4 or IPv6)

    Returns:
        True if the address is private/loopback/reserved, False otherwise
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        return False


def _validate_url_not_ssrf(url_str: str) -> None:
    """Validate that a URL does not point to internal/private addresses.

    Raises ValueError if the URL could be used for SSRF attacks.
    This is a defense-in-depth measure; infrastructure-level egress
    controls should also be in place.

    Args:
        url_str: URL string to validate

    Raises:
        ValueError: If URL contains private/internal addresses
    """
    parsed = urlparse(url_str.lower())
    hostname = parsed.hostname or ""

    # Strip brackets from IPv6 addresses
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]

    # Block known internal hostnames
    if hostname in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(
            f"server_url cannot point to internal host: {hostname}. "
            "Use a public hostname for MCP servers."
        )

    # Block private/reserved IP addresses
    if _is_private_or_reserved_ip(hostname):
        raise ValueError(
            f"server_url cannot point to private/reserved IP: {hostname}. "
            "Use a public hostname for MCP servers."
        )

    # Block localhost-related patterns (e.g., 127.0.0.1.nip.io)
    localhost_patterns = re.compile(r"^(127\.\d+\.\d+\.\d+|localhost|\[?::1\]?)")
    if localhost_patterns.match(hostname):
        raise ValueError(
            f"server_url cannot point to localhost: {hostname}. "
            "Use a public hostname for MCP servers."
        )


class MCPConfiguration(BaseModel):
    """MCP (Model Context Protocol) configuration for template.

    When enabled, the agent connects to the Storefront MCP to fetch available tools.
    MCP tools are registered with the LLM and can be called during the conversation.

    Example:
        {
            "enabled": true,
            "server_url": "https://your-store.myshopify.com/api/mcp",
            "timeout": 30
        }
    """

    enabled: bool = Field(False, description="Whether MCP is enabled for this template")
    server_url: HttpUrl = Field(
        description="Storefront MCP URL (e.g., https://shop.myshopify.com/api/mcp)",
    )
    timeout: int = Field(
        30, ge=5, le=120, description="Connection timeout in seconds (5-120)"
    )
    headers: Optional[Dict[str, SecretStr]] = Field(
        None,
        description="Optional headers for MCP requests (values are secrets, e.g., API tokens)",
    )

    @model_validator(mode="after")
    def validate_mcp_config(self) -> "MCPConfiguration":
        """Validate that enabled MCP config has required fields and prevent SSRF.

        Validates:
        - server_url is required when enabled
        - server_url uses HTTPS
        - server_url does not point to private/internal addresses (SSRF protection)

        Note: SSRF validation here is defense-in-depth. Infrastructure-level
        egress controls should also be configured to restrict outbound traffic.
        """
        if self.enabled:
            if not self.server_url:
                raise ValueError("server_url is required when MCP is enabled")

            url_str = str(self.server_url)

            if not url_str.lower().startswith("https://"):
                raise ValueError("server_url must use HTTPS for security")

            _validate_url_not_ssrf(url_str)

        return self


class ConfigurationModel(BaseModel):
    tts_voice_name: Optional[TTSVoiceName] = None
    mira_voice_id: Optional[str] = (
        None  # DEPRECATED: Use cartesia_voice_configurations.voice_id instead
    )
    cartesia_voice_configurations: Optional[CartesiaVoiceConfiguration] = (
        None  # Cartesia voice configuration (overrides global defaults)
    )
    elevenlabs_voice_configurations: Optional[ElevenLabsVoiceConfiguration] = (
        None  # ElevenLabs voice configuration (overrides global defaults)
    )
    tts_selection_config: Optional[TTSSelectionConfig] = (
        None  # LLM-based TTS provider selection config
    )
    stt_language: Optional[str] = None
    soniox_context: Optional[str] = Field(
        None,
        description="Soniox STT context for speech recognition domain adaptation (e.g., business terms, product names). Overrides BREEZE_BUDDY_SONIOX_CONTEXT env var if set.",
    )
    payload_based_language_selection: bool = False
    enable_background_sound: bool = False
    background_sound_file: Optional[BackgroundSoundFile] = None
    background_sound_volume: float = 2.0
    initial_greeting: Optional[str] = (
        None  # Initial greeting text template with variables (e.g., "Hi {customer_name}")
    )
    ivr_greeting: Optional[str] = (
        None  # Full IVR audio text including greeting and menu options (e.g., "Welcome to support. Press 1 for billing, press 2 for technical support")
    )
    ivr_goodbye: Optional[str] = (
        None  # Goodbye message when no input received (default: "We didn't receive your input. Goodbye.")
    )
    ivr_priority: Optional[int] = Field(
        None,
        ge=1,
        description="Priority order for IVR menu (lower number = earlier in menu). Gaps allowed (e.g., 1, 3, 4).",
    )
    transfer_number: Optional[str] = Field(
        None, description="Phone number to transfer the call to"
    )
    vad_config: Optional[VadConfig] = Field(
        None, description="Default VAD configuration for the template"
    )
    enable_inbound: bool = False  # Whether this template can handle inbound calls
    user_idle_configuration: Optional[UserIdleHandlingConfig] = (
        None  # User idle handling config
    )
    mcp_config: Optional[List[MCPConfiguration]] = Field(
        None,
        description=(
            "MCP server configurations for template-based tool integration. "
            "Supports multiple MCP servers as a list. "
            "Backward compatible with single object format."
        ),
    )

    @field_validator("mcp_config", mode="before")
    @classmethod
    def _normalize_mcp_config(cls, v: Any) -> Any:
        """Normalize single MCP config object to list for backward compatibility."""
        if isinstance(v, dict):
            return [v]
        return v

    noise_filter: Optional[NoiseFilterConfig] = Field(
        None, description="Noise filter configuration for audio input processing"
    )
    keyword_filter: Optional[KeywordFilterConfig] = Field(
        None,
        description="Keyword filter to suppress specific transcriptions while bot is active",
    )


class FlowAction(BaseModel):
    type: ActionType
    text: Optional[str] = None
    handler: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


class TaskMessage(BaseModel):
    role: str
    content: str


class FieldSource(str, Enum):
    """Source types for field value resolution.

    Used by both hooks (fire-and-forget) and global HTTP functions (wait for response).
    - STATIC: Value is a literal or contains {template_var} placeholders
    - LLM: Value comes from LLM function arguments
    - COMPUTED: Value is dynamically computed at invocation time (e.g., timestamps)
    """

    STATIC = "static"
    LLM = "llm"
    COMPUTED = "computed"


class HttpMethod(str, Enum):
    """HTTP methods supported for external API calls"""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class HttpAuthType(str, Enum):
    """Authentication types for HTTP requests"""

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    API_KEY = "api_key"


class HttpAuthConfig(BaseModel):
    """Authentication configuration for HTTP requests"""

    type: HttpAuthType = HttpAuthType.NONE
    token: Optional[SecretStr] = None  # For bearer auth
    username: Optional[str] = None  # For basic auth
    password: Optional[SecretStr] = None  # For basic auth
    api_key_name: Optional[str] = None  # Header name for API key
    api_key_value: Optional[SecretStr] = None  # API key value


class HttpRequestConfig(BaseModel):
    """Complete HTTP request configuration for hooks and global functions.

    The body field can be:
    - A Dict that will be serialized to JSON
    - A JSON string that will be parsed and have placeholders resolved
    """

    url: str
    method: HttpMethod = HttpMethod.POST
    headers: Dict[str, str] = {}
    query_params: Dict[str, str] = {}
    body: Optional[Dict[str, Any] | str] = None
    auth: Optional[HttpAuthConfig] = None
    timeout: int = 10
    max_retries: int = 3


class FieldConfig(BaseModel):
    """Configuration for a single field in hooks or global HTTP functions.

    Defines how to resolve a field value:
    - source: Where the value comes from (STATIC, LLM, or COMPUTED)
    - value: For STATIC, the literal value or {template_var} placeholder.
             For LLM, the name of the argument from LLM function call.
             For COMPUTED, the function expression (e.g., "utc_now_minus_hours:1").
    """

    source: FieldSource
    value: Optional[Any] = None


class HookConfig(BaseModel):
    """Configuration for a hook with expected fields"""

    name: str
    expected_fields: Dict[str, FieldConfig] = {}
    http_request: Optional[HttpRequestConfig] = None  # For send_http_request hook


class FlowFunction(BaseModel):
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    transition_to: Optional[str] = None
    hooks: List[HookConfig] = []


class GlobalFunctionType(str, Enum):
    """Types of global functions supported by the system."""

    HTTP = "http"
    BUILTIN = "builtin"  # Built-in handlers (e.g., warm transfer, get current time)
    CUSTOM = "custom"  # Future: custom Python function handlers


class BaseGlobalFunction(BaseModel):
    """Base model for all global function types.

    Subclasses should define their specific configuration fields.

    Note: func_pre_actions / func_post_actions use aliases so that existing
    JSON templates with 'pre_actions' / 'post_actions' keys on global functions
    continue to work. In Python code, always use the func_ prefixed names.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: GlobalFunctionType
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    func_pre_actions: List[FlowAction] = Field(default=[], alias="pre_actions")
    func_post_actions: List[FlowAction] = Field(default=[], alias="post_actions")


class GlobalHttpFunction(BaseGlobalFunction):
    """
    Configuration for a global HTTP function available across all nodes.

    Global functions are registered with FlowManager and can be called by the LLM
    from any node in the conversation. Unlike hooks (fire-and-forget), global
    functions wait for the HTTP response and return data to the LLM.

    Example:
        {
            "name": "check_order_status",
            "description": "Check the status of a customer order",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "expected_fields": {
                "order_id": {"source": "llm", "value": "order_id"},
                "api_key": {"source": "static", "value": "sk-123"}
            },
            "http_request": {
                "method": "GET",
                "url": "https://api.example.com/orders/{order_id}",
                "auth": {"type": "bearer", "token": "{api_key}"}
            }
        }
    """

    type: GlobalFunctionType = GlobalFunctionType.HTTP
    expected_fields: Dict[str, FieldConfig] = {}
    http_request: HttpRequestConfig


class GlobalBuiltinFunction(BaseGlobalFunction):
    """
    Configuration for a built-in global function available across all nodes.

    Built-in functions are internal handlers (e.g., warm transfer, get current time)
    that can be exposed as global functions via template configuration. The `handler`
    field maps to a key in the builtin handler registry.

    Example:
        {
            "type": "builtin",
            "name": "transfer_to_agent",
            "handler": "connect_to_live_agent",
            "description": "Transfer the call to a human agent when requested"
        }
    """

    type: GlobalFunctionType = GlobalFunctionType.BUILTIN
    handler: str = Field(
        ...,
        description="Key in the builtin handler registry (e.g., 'connect_to_live_agent', 'get_current_time')",
    )
    pre_tts_message: Optional[str] = Field(
        None,
        description="TTS message to speak and wait for completion BEFORE executing the handler. "
        "Useful for handlers that terminate the pipeline (e.g., transfer) where the LLM's "
        "generated text may get cut off.",
    )


class FlowNodeModel(BaseModel):
    node_name: str
    task_messages: List[TaskMessage]
    role_messages: List[TaskMessage] = []
    pre_actions: List[FlowAction] = []
    post_actions: List[FlowAction] = []
    functions: List[FlowFunction] = []
    vad_config: Optional[VadConfig] = Field(
        None, description="Node-specific VAD configuration (overrides template VAD)"
    )


class TemplateModel(BaseModel):
    # Read-only fields (set by server, not editable via API).
    # These are intentionally excluded from ReplaceTemplateRequest so that
    # a GET response can be sent directly to PUT — extra fields are auto-stripped.
    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    # Editable fields (these match ReplaceTemplateRequest field names 1:1).
    name: str
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True


# Request models for API
class RequestFlowFunction(BaseModel):
    function_name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    transition_to: Optional[str] = None
    hooks: List[HookConfig] = []


class RequestFlowNode(BaseModel):
    node_name: str
    task_messages: List[Dict[str, Any]] = []
    role_messages: List[Dict[str, Any]] = []
    pre_actions: List[Dict[str, Any]] = []
    post_actions: List[Dict[str, Any]] = []
    functions: List[RequestFlowFunction] = []


class CreateTemplateRequest(BaseModel):
    # New field names
    reseller_id: str
    name: str
    merchant_id: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None


class ReplaceTemplateRequest(BaseModel):
    """Request model for updating a template via PUT.

    IMPORTANT — GET-to-PUT contract:
    This model uses extra="ignore" so that a GET /templates/{id} response can be
    sent directly to PUT /templates/{id} after editing. Read-only fields returned
    by GET (id, merchant_id, created_at, updated_at) are automatically stripped.
    When adding new read-only fields to TemplateModel, do NOT add them here —
    they will be safely ignored. When adding new editable fields, add them to
    BOTH TemplateModel and this model with the SAME field name.

    Non-nullable fields (name, flow, is_active) must be provided - throws 400 if not.
    Nullable fields (shop_identifier, outbound_number_id, expected_payload_schema,
    expected_callback_response_schema, configurations) - if not provided, set to NULL.
    """

    # extra="ignore" allows clients to pass the full GET response body to PUT;
    # read-only fields (id, merchant_id, created_at, updated_at) are auto-stripped.
    model_config = ConfigDict(extra="ignore")

    name: str
    merchant_id: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
