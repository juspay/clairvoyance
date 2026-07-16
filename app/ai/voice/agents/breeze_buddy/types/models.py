from io import BytesIO
from typing import Any, Dict, List, NamedTuple, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from app.schemas.breeze_buddy.core import ExecutionMode

# TEMPORARY HACK — remove once redbus migrates to template_id.
# redbus is the one legacy merchant still sending the removed `template`
# (name) field in lead pushes. This is NOT name-based template resolution:
# it is a hardcoded alias applied only on an exact name match. Do not add
# other merchants or names here.
_REDBUS_LEGACY_TEMPLATE_ALIASES: Dict[str, str] = {
    "redbus-refund-eligibility-verification": "12d435c1-d044-40c9-8c2b-f9a379e8f871",
}


class CallRecordingResult(NamedTuple):
    audio_file: BytesIO
    is_daily: bool


class PushLeadRequest(BaseModel):
    """Lead push request. Templates are identified by id ONLY — name-based
    resolution was removed; a legacy ``template`` field in the body is
    ignored (pydantic drops unknown fields). Sole exception: the hardcoded
    redbus alias in ``_REDBUS_LEGACY_TEMPLATE_ALIASES`` above."""

    request_id: str
    payload: Dict[str, Any]
    template_id: str

    @model_validator(mode="before")
    @classmethod
    def _apply_redbus_legacy_template_alias(cls, data: Any) -> Any:
        # TEMPORARY HACK — see _REDBUS_LEGACY_TEMPLATE_ALIASES.
        if isinstance(data, dict) and not data.get("template_id"):
            template_name = data.get("template")
            if isinstance(template_name, str):
                alias = _REDBUS_LEGACY_TEMPLATE_ALIASES.get(template_name)
                if alias:
                    data["template_id"] = alias
        return data

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
