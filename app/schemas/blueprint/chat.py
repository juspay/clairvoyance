"""
Pydantic schemas for Blueprint agent chat messages.
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    step: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SendMessageRequest(BaseModel):
    content: str
    metadata: Optional[dict[str, Any]] = None  # approval, feedback, model_choice


class SendMessageResponse(BaseModel):
    messages: list[ChatMessage]
    current_step: Optional[str] = None
    approval_required: bool = False
    preview: Optional[dict[str, Any]] = None


class WebSocketMessage(BaseModel):
    type: str  # message, approve, reject, select_model, step_update, approval_required, error, template_created
    content: Optional[str] = None
    step: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
