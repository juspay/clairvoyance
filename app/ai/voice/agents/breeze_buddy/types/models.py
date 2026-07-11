from io import BytesIO
from typing import Any, Dict, List, NamedTuple, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.schemas.breeze_buddy.core import ExecutionMode


class CallRecordingResult(NamedTuple):
    audio_file: BytesIO
    is_daily: bool


class PushLeadRequest(BaseModel):
    """Lead push request. Templates are identified by id ONLY — name-based
    resolution was removed; a legacy ``template`` field in the body is
    ignored (pydantic drops unknown fields)."""

    request_id: str
    payload: Dict[str, Any]
    template_id: str

    @field_validator("template_id")
    @classmethod
    def validate_template_id_format(cls, v: str) -> str:
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


class LoginRequest(BaseModel):
    username: str
    password: str


class LeadCancellation(BaseModel):
    lead_id: str
    cancellation_reason: str


class CancelLeadRequest(BaseModel):
    leads: List[LeadCancellation]
