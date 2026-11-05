"""The evaluation vocabulary and the shapes of the generic evaluations API
(/templates/{id}/evaluations*): ``EvaluationType`` names what an
evaluation_config row is, one surface serves every type."""

from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class EvaluationType(str, Enum):
    TOPIC = "TOPIC"
    CONVERSATION_EVALS = "CONVERSATION_EVALS"


class EvaluationEnableRequest(BaseModel):
    evaluation_type: EvaluationType
    enabled: bool


class EvaluationConfigResponse(BaseModel):
    template_id: UUID
    evaluation_type: EvaluationType
    enabled: bool
    # admin-only; None for other roles
    configuration: Optional[Dict[str, Any]] = None
    # the TOPIC catalog (a separate column); empty for other types
    topics: List[str] = Field(default_factory=list)


class SaveEvaluationConfigurationRequest(BaseModel):
    """The full configuration; the handler validates it via the type's own
    validator, then creates the agent's row (disabled) or replaces the
    configuration of the existing one (CONVERSATION_EVALS only for now)."""

    evaluation_type: EvaluationType
    configuration: Dict[str, Any]
