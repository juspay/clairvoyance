"""Connector-account shapes: the door, the pipes under it, and what a
provider's handshake produced."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ConnectorInstallation(BaseModel):
    """A merchant's account on one connector — the door.

    Holds no secret: ``credential_id`` says where the bundle lives.
    """

    id: str
    merchant_id: str
    connector_key: str
    external_account_id: str
    display_label: Optional[str] = None
    credential_id: Optional[str] = None
    status: str
    token_expires_at: Optional[datetime] = None


class ChannelBinding(BaseModel):
    """One real endpoint under an installation — the pipe.

    ``address`` is the provider's identifier for it (a Meta phone_number_id,
    a sender id, a from-address); what it means is the channel's business.
    """

    id: str
    merchant_id: str
    channel: str
    installation_id: str
    address: str
    capabilities: Dict[str, Any] = {}
    is_primary: bool = False
    status: str


# ---------------------------------------------------------------------------
# Connector onboarding — the shapes crossing the ConnectorOnboarder port
# ---------------------------------------------------------------------------

#: Canon T11's health ladder. A provider face reports how far up it got; only
#: onboarding.py turns that into a row status, so "how healthy" and "what the
#: traffic light says" cannot drift apart per connector.
HealthLevel = Literal["configured", "authenticated", "subscribed", "healthy"]


class OnboardResult(BaseModel):
    """What a provider's handshake produced — facts, no database.

    Everything vendor-shaped has already been spent by the time this crosses
    the port: an Embedded Signup code became a token, a phone_number_id was
    checked against its WABA, a webhook subscription was attempted. What
    comes back is what any connector's onboarding needs.
    """

    #: The provider's own id for the account — a WABA id today.
    external_account_id: str
    #: The endpoint under it that becomes the binding's address (a Meta
    #: phone_number_id). What it means is the channel's business.
    #:
    #: None for a connector with no channel — a door with no pipe, which
    #: ConnectorSpec.channel already models as Optional and _onboard_in_txn
    #: already returns early for.
    address: Optional[str] = None
    #: What the merchant calls this account in the console. Cosmetic, and it
    #: rides the RESULT rather than being read off the request by generic
    #: code, because only the connector knows which of its request fields is
    #: the human-facing name.
    display_label: Optional[str] = None
    #: The whole credential bundle, ready for the vault. Never logged.
    bundle: Dict[str, Any] = Field(default_factory=dict, repr=False)
    #: None means the provider issues a permanent credential — an HONEST
    #: NULL, not a missing value. Canon T11 col 9: the refresh job watches
    #: non-NULL rows, so a wrong NULL is a connector that dies silently.
    token_expires_at: Optional[datetime] = None
    health_level: HealthLevel = "authenticated"
    #: Mandatory below 'healthy' (canon T11): a light that is not green must
    #: carry the sentence explaining it.
    health_why: Optional[str] = None

    @model_validator(mode="after")
    def _why_is_mandatory_below_healthy(self) -> "OnboardResult":
        """canon T11's rule, enforced where it cannot be forgotten.

        status is the traffic light and health_detail is the sentence under
        it. A door that comes back amber with nothing written in `why` gives
        the connections screen a colour and no reason, and whoever looks at
        it next has to reconstruct what failed from logs. Refusing here costs
        a provider face one line; leaving it to a comment costs that person
        an afternoon.
        """
        if self.health_level != "healthy" and not (self.health_why or "").strip():
            raise ValueError(
                f"health_level '{self.health_level}' is below 'healthy' and "
                f"needs a health_why explaining it"
            )
        return self


class InstallationRead(BaseModel):
    """One connector account, as the console sees it.

    ``credential_id`` is deliberately absent: a read model that names where a
    secret lives is one screenshot away from being a map to it.
    """

    id: str
    merchant_id: str
    connector_key: str
    external_account_id: str
    display_label: Optional[str] = None
    status: str
    token_expires_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    health_detail: Dict[str, Any] = Field(default_factory=dict)
    installed_at: datetime
    created_at: datetime
    updated_at: datetime
