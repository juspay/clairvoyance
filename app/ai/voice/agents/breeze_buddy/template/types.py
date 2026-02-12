"""
Pydantic models for the dynamic workflow engine.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, SecretStr


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
    payload_based_language_selection: bool = False
    enable_background_sound: bool = False
    background_sound_file: Optional[BackgroundSoundFile] = None
    background_sound_volume: float = 2.0
    initial_greeting: Optional[str] = (
        None  # Initial greeting text template with variables (e.g., "Hi {customer_name}")
    )
    ivr_greeting: Optional[str] = (
        None  # Greeting prefix for IVR menu (e.g., "Hello, this is Rhea from Namma Yatri support")
    )
    ivr_description: Optional[str] = (
        None  # Description for IVR menu (e.g., "Trip feedback in English")
    )
    ivr_priority: Optional[int] = Field(
        None,
        ge=1,
        description="Priority order for IVR menu (lower number = earlier in menu). Gaps allowed (e.g., 1, 3, 4).",
    )
    vad_config: Optional[VadConfig] = Field(
        None, description="Default VAD configuration for the template"
    )
    enable_inbound: bool = False  # Whether this template can handle inbound calls
    user_idle_configuration: Optional[UserIdleHandlingConfig] = (
        None  # User idle handling config
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
    """

    STATIC = "static"
    LLM = "llm"


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
    - source: Where the value comes from (STATIC or LLM)
    - value: For STATIC, the literal value or {template_var} placeholder.
             For LLM, the name of the argument from LLM function call.
    """

    source: FieldSource  # "static" or "llm"
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
    CUSTOM = "custom"  # Future: custom Python function handlers


class BaseGlobalFunction(BaseModel):
    """Base model for all global function types.

    Subclasses should define their specific configuration fields.
    """

    type: GlobalFunctionType
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []


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
    id: str
    merchant_id: str
    shop_identifier: Optional[str] = None
    name: str
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True
    rendered_system_prompt: str = ""
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


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
    merchant: str
    template_name: str
    identifier: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None


class ReplaceTemplateRequest(BaseModel):
    """Request model for updating a template.

    Non-nullable fields (name, flow, is_active) must be provided - throws 400 if not.
    Nullable fields (identifier, outbound_number_id, expected_payload_schema,
    expected_callback_response_schema, configurations) - if not provided, set to NULL.
    """

    name: str
    identifier: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
