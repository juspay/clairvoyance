"""
Pydantic models for the dynamic workflow engine.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ActionType(str, Enum):
    TTS_SAY = "tts_say"
    END_CONVERSATION = "end_conversation"
    FUNCTION = "function"


class TTSVoiceName(str, Enum):
    RHEA = "rhea"
    SARA = "sara"
    MIRA = "mira"


class BackgroundSoundFile(str, Enum):
    """Enum for available background sound files"""

    OFFICE_AMBIENCE = "office-ambience"


class ConfigurationModel(BaseModel):
    tts_voice_name: Optional[TTSVoiceName] = None
    stt_language: Optional[str] = None
    payload_based_language_selection: bool = False
    enable_background_sound: bool = False
    background_sound_file: Optional[BackgroundSoundFile] = None
    background_sound_volume: float = 2.0
    initial_greeting: Optional[str] = (
        None  # Initial greeting text template with variables (e.g., "Hi {customer_name}")
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


class UpdateTemplateRequest(BaseModel):
    """
    Request model for updating a template.
    All fields are optional - only provided fields will be updated.
    """

    template_name: Optional[str] = None
    identifier: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None
    flow: Optional[Dict[str, Any]] = None
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
