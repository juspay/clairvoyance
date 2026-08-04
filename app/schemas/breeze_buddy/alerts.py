"""Alert-related request/response schemas."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AlertGroup(BaseModel):
    """Alert group model matching the alert_groups DB table."""

    id: str
    name: str
    reseller_id: str
    members: List[Dict[str, str]]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AlertFireRequest(BaseModel):
    """Request body for POST /alerts/fire.

    Note: reseller_id is NOT in the body — it comes from the JWT token claims.
    This prevents reseller impersonation. The token must be scoped to exactly
    one reseller (reseller_ids must have a single entry, not ["*"]).
    """

    # Dedup key — must be unique per alert scenario.
    # OpenObserve: use {alert_name} template variable (e.g. "stt-degraded")
    # Internal: use a descriptive ID (e.g. "stt-degraded-<epoch_bucket>")
    alert_id: str = Field(
        ...,
        min_length=1,
        description="Unique alert identifier for deduplication",
        examples=["stt-degraded", "tts-elevenlabs-down"],
    )

    # Which group of people to call
    alert_group_name: str = Field(
        ...,
        min_length=1,
        description="Name of the alert group (must exist in alert_groups table for this reseller)",
        examples=["platform-oncall"],
    )

    # Human-readable message spoken via TTS
    # The caller builds this string — the endpoint just passes it through
    alert_message: str = Field(
        ...,
        min_length=1,
        description="Alert message spoken via TTS on the call",
        examples=["STT degraded: 47 errors in 5 minutes"],
    )

    # BB template config — fully reuses existing template machinery
    # merchant_id is optional and validated against the token's merchant_ids scope
    merchant_id: Optional[str] = Field(
        None,
        description="Merchant ID (optional). Validated against JWT token scope.",
    )
    template: str = Field(
        ...,
        min_length=1,
        description="BB template name (must exist in templates table)",
        examples=["alert-voice-notification"],
    )

    # How long (seconds) to suppress duplicate alerts for this alert_id
    dedup_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description="Dedup window in seconds. 0 = no dedup.",
    )

    # Extra payload fields merged into the lead payload
    extra_payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional fields to include in the lead payload",
    )
