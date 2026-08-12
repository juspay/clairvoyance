from io import BytesIO
from typing import Any, Dict, List, NamedTuple, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.breeze_buddy.core import ExecutionMode

# TEMPORARY HACK — remove once legacy merchants migrate to template_id.
# A few legacy merchants still send the removed `template` (name) field in
# lead pushes. This is NOT name-based template resolution: it is a hardcoded
# alias applied only on an exact name match when template_id is absent.
_LEGACY_TEMPLATE_NAME_ALIASES: Dict[str, str] = {
    "redbus-refund-eligibility-verification": "12d435c1-d044-40c9-8c2b-f9a379e8f871",
    "loan_reminder_dpd_0": "1079319f-7c90-4197-94fc-6d8fb4af24c7",
    "loan_collections_dpd_0_7_demo": "6742efc7-9385-4f25-94c2-1a8386797e3d",
}


class CallRecordingResult(NamedTuple):
    audio_file: BytesIO
    is_daily: bool


class PushLeadRequest(BaseModel):
    """Lead push request. Templates are identified by id ONLY — name-based
    resolution was removed; a legacy ``template`` field in the body is
    ignored (pydantic drops unknown fields). Sole exception: the hardcoded
    aliases in ``_LEGACY_TEMPLATE_NAME_ALIASES`` above."""

    request_id: str
    payload: Dict[str, Any]
    template_id: str

    @model_validator(mode="before")
    @classmethod
    def _apply_legacy_template_name_alias(cls, data: Any) -> Any:
        # TEMPORARY HACK — see _LEGACY_TEMPLATE_NAME_ALIASES.
        if isinstance(data, dict) and not data.get("template_id"):
            template_name = data.get("template")
            if isinstance(template_name, str):
                alias = _LEGACY_TEMPLATE_NAME_ALIASES.get(template_name)
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
    # Optional scheduling delay in seconds, added on top of the
    # template's call-execution initial_offset when computing
    # next_attempt_at. Omit (or pass 0) for no delay; negative
    # values are rejected (422).
    delay: int = Field(default=0, ge=0)


class LoginRequest(BaseModel):
    username: str
    password: str


class LeadCancellation(BaseModel):
    lead_id: str
    cancellation_reason: str


class CancelLeadRequest(BaseModel):
    leads: List[LeadCancellation]
