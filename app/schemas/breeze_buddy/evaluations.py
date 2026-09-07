"""Request and response contracts for per-template evaluations."""

from typing import Any, Dict, List
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.breeze_buddy.conversation_analysis import EvaluationType


class EvaluationConfigurationUpdateRequest(BaseModel):
    """Replace the complete type-specific evaluation configuration."""

    configuration: Dict[str, Any]


class EvaluationConfigurationResponse(BaseModel):
    """Canonical envelope shared by evaluation configuration types."""

    template_id: UUID
    evaluation_type: EvaluationType
    enabled: bool
    topics: List[str] = Field(default_factory=list)
    configuration: Dict[str, Any]
