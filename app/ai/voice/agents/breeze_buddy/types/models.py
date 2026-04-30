from io import BytesIO
from typing import Any, Dict, List, NamedTuple, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from app.schemas.breeze_buddy.core import ExecutionMode


class CallRecordingResult(NamedTuple):
    audio_file: BytesIO
    is_daily: bool


class PushLeadRequest(BaseModel):
    request_id: str
    payload: Dict[str, Any]
    template: Optional[str] = None
    template_id: Optional[str] = None

    @field_validator("template_id")
    @classmethod
    def validate_template_id_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                UUID(v)
            except ValueError as e:
                raise ValueError("template_id must be a valid UUID") from e
        return v

    reseller_id: str
    merchant_id: Optional[str] = None
    reporting_webhook_url: str | None = None
    execution_mode: Optional[ExecutionMode] = (
        None  # Defaults to TELEPHONY if not provided
    )
    # Playground mode: when true, uses configurations_override
    is_playground: Optional[bool] = False
    configurations_override: Optional[Dict[str, Any]] = (
        None  # Override template configurations
    )
    flow_override: Optional[Dict[str, Any]] = (
        None  # Override template flow JSON (playground only)
    )

    @model_validator(mode="after")
    def check_template_identifier(self) -> "PushLeadRequest":
        if not self.template and not self.template_id:
            raise ValueError("Either 'template' or 'template_id' must be provided")
        return self


class LoginRequest(BaseModel):
    username: str
    password: str


class LeadCancellation(BaseModel):
    lead_id: str
    cancellation_reason: str


class CancelLeadRequest(BaseModel):
    leads: List[LeadCancellation]
