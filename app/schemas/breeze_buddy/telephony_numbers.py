"""
Schemas for telephony number search and purchase operations.

Provider-agnostic naming where possible so future providers
(Twilio buy, Exotel buy) can reuse the same response shapes.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ── Search ──────────────────────────────────────────────────────────────────


class TelephonyNumberSearchParams(BaseModel):
    """Query parameters for searching available numbers."""

    country_iso: str = Field(
        default="IN",
        description="ISO 3166 alpha-2 country code (default: IN for India)",
    )
    type: Optional[str] = Field(
        default=None,
        description="Number type: tollfree, local, mobile, national, fixed",
    )
    pattern: Optional[str] = Field(
        default=None,
        description="Number pattern to match (e.g., '022' for Mumbai prefix)",
    )
    services: Optional[str] = Field(
        default=None,
        description="Filter by capabilities: voice, sms, voice,sms",
    )
    region: Optional[str] = Field(
        default=None,
        description="Region name (e.g., 'Mumbai'). For fixed type only.",
    )
    limit: int = Field(default=20, ge=1, le=20, description="Results per page (max 20)")
    offset: int = Field(default=0, ge=0, description="Pagination offset")


class AvailableTelephonyNumber(BaseModel):
    """A single available number from search results."""

    number: str
    prefix: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    type: Optional[str] = None
    sub_type: Optional[str] = None
    setup_rate: Optional[str] = None
    monthly_rental_rate: Optional[str] = None
    sms_enabled: bool = False
    voice_enabled: bool = False
    voice_rate: Optional[str] = None
    sms_rate: Optional[str] = None
    restriction: Optional[str] = None
    restriction_text: Optional[str] = None
    resource_uri: Optional[str] = None


class TelephonyNumberSearchMeta(BaseModel):
    """Pagination metadata from search."""

    limit: int = 20
    offset: int = 0
    total_count: int = 0
    inr_conversion_rate: Optional[float] = Field(
        default=None,
        description="Conversion rate from USD to INR for this provider",
    )


class TelephonyNumberSearchResponse(BaseModel):
    """Response for number search endpoint."""

    numbers: List[AvailableTelephonyNumber] = Field(default_factory=list)
    meta: TelephonyNumberSearchMeta = Field(default_factory=TelephonyNumberSearchMeta)


# ── Buy ─────────────────────────────────────────────────────────────────────


class TelephonyNumberBuyRequest(BaseModel):
    """Request to buy a number and register in telephony_numbers table."""

    number: str = Field(
        ...,
        description="Phone number to purchase from the provider (from search results)",
    )
    reseller_id: Optional[str] = Field(
        default=None,
        description=(
            "Reseller ID to assign this number to. Required for admin callers; "
            "resellers and merchants have it derived from their own scope and "
            "may omit it."
        ),
    )
    merchant_id: Optional[str] = Field(
        default=None,
        description=(
            "Merchant ID to assign. Optional for admin/reseller (umbrella-owned "
            "if omitted). For merchants with more than one merchant in scope, "
            "required -- otherwise defaults to their single merchant."
        ),
    )
    maximum_channels: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum concurrent call channels for this number",
    )


class ProviderBuyResult(BaseModel):
    """Normalized result of a provider purchase call.

    Each provider adapter maps its raw SDK response onto this shape so the
    handler never has to know provider-specific response formats.
    """

    status: str = Field(
        default="unknown",
        description="Provider purchase status. 'fulfilled' means success.",
    )
    api_id: Optional[str] = Field(
        default=None, description="Provider API request ID, for support/audit"
    )
    message: str = Field(default="", description="Provider-supplied status message")
    numbers: List[Dict[str, Any]] = Field(
        default_factory=list, description="Per-number purchase results"
    )

    @property
    def is_fulfilled(self) -> bool:
        return self.status == "fulfilled"


class TelephonyNumberBuyResponse(BaseModel):
    """Response for number buy endpoint.

    Contains both the provider purchase status and the created telephony_number record.
    """

    provider_status: str = Field(
        description="Provider purchase status (e.g., 'fulfilled')"
    )
    provider_api_id: Optional[str] = Field(
        default=None, description="Provider API request ID"
    )
    telephony_number: Dict[str, Any] = Field(
        description="Created telephony_number record"
    )
    message: str = Field(description="Human-readable status message")
