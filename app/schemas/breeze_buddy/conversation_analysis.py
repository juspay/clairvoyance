"""Models for post-conversation evaluations."""

from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.ai.voice.llm import LLMProvider, LLMSdk


class ConversationChannel(str, Enum):
    VOICE = "VOICE"
    CHAT = "CHAT"


class EvaluationType(str, Enum):
    TOPIC = "TOPIC"
    GUARDRAIL = "GUARDRAIL"


class ConversationEvaluationJob(BaseModel):
    source_id: str = Field(min_length=1, max_length=255)
    channel: ConversationChannel
    template_id: UUID


class ConversationTopic(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    phrase: str = Field(default="", max_length=500)
    evidence_turns: List[int] = Field(default_factory=list)


class TopicExtractionResult(BaseModel):
    topics: List[ConversationTopic] = Field(default_factory=list)


class TopicEvaluationSettingsRequest(BaseModel):
    enabled: bool


class TopicCatalogResponse(BaseModel):
    template_id: UUID
    enabled: bool
    topics: List[str] = Field(default_factory=list)


class UpdateTopicConfigurationRequest(BaseModel):
    provider: Optional[LLMProvider] = None
    sdk: Optional[LLMSdk] = None
    model: Optional[str] = Field(None, max_length=200)
    region: Optional[str] = Field(None, max_length=100)
    system_prompt: Optional[str] = Field(None, max_length=50000)
    settings: Optional[Dict[str, Any]] = None

    @field_validator(
        "provider",
        "sdk",
        "model",
        "region",
        "system_prompt",
        "settings",
        mode="before",
    )
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit fields that should not change")
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value


class TopicConfigurationResponse(BaseModel):
    template_id: UUID
    provider: LLMProvider
    sdk: Optional[LLMSdk] = None
    model: str
    region: Optional[str] = None
    system_prompt: str
    settings: Dict[str, Any]


class ConversationTopicsResponse(BaseModel):
    topics: List[ConversationTopic] = Field(default_factory=list)
