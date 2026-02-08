from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.schemas.breeze_buddy.core import ExecutionMode


class PushLeadRequest(BaseModel):
    request_id: str
    payload: Dict[str, Any]
    template: str
    merchant: str
    identifier: Optional[str] = None
    reporting_webhook_url: str | None = None
    execution_mode: Optional[ExecutionMode] = (
        None  # Defaults to TELEPHONY if not provided
    )


class LoginRequest(BaseModel):
    username: str
    password: str


class LeadCancellation(BaseModel):
    lead_id: str
    cancellation_reason: str


class CancelLeadRequest(BaseModel):
    leads: List[LeadCancellation]
