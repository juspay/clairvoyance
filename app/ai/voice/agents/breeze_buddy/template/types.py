"""
Pydantic models for the dynamic workflow engine.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class TTSVoiceName(str, Enum):
    RHEA = "rhea"
    SARA = "sara"
    MIRA = "mira"


class BackgroundSoundFile(str, Enum):
    """Enum for available background sound files"""

    OFFICE_AMBIENCE = "office-ambience"


class ConfigurationModel(BaseModel):
    tts_voice_name: Optional[TTSVoiceName] = None
    mira_voice_id: Optional[str] = None  # Custom Cartesia voice ID per template
    stt_language: Optional[str] = None
    payload_based_language_selection: bool = False
    enable_background_sound: bool = False
    background_sound_file: Optional[BackgroundSoundFile] = None
    background_sound_volume: float = 2.0
    initial_greeting: Optional[str] = (
        None  # Initial greeting text template with variables (e.g., "Hi {customer_name}")
    )
    vad_config: Optional[VadConfig] = Field(
        None, description="Default VAD configuration for the template"
    )


class FlowAction(BaseModel):
    type: ActionType
    text: Optional[str] = None
    handler: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


class TaskMessage(BaseModel):
    role: str
    content: str


class HookFieldConfigSource(str, Enum):
    STATIC = "static"
    LLM = "llm"


class HookFieldConfig(BaseModel):
    """Configuration for a single field in a hook"""

    source: HookFieldConfigSource  # "static" or "llm"
    value: Optional[Any] = None  # Used when source is "static"


class HookConfig(BaseModel):
    """Configuration for a hook with expected fields"""

    name: str
    expected_fields: Dict[str, HookFieldConfig] = {}


class FlowFunction(BaseModel):
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    transition_to: Optional[str] = None
    hooks: List[HookConfig] = []


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
    description: Optional[str] = None
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None


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
