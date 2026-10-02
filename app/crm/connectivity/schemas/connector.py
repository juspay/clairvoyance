"""Connector-account shapes: the door, the pipes under it, and what a
provider's handshake produced."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.crm.connectivity.schemas.tenancy import TenantScoped


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


class SignupConfig(BaseModel):
    """What a browser needs to open one connector's signup popup —
    GET /connectors/{key}/signup.

    Everything here is PUBLIC by design (an app id, a signup configuration
    id, an API version): the browser hands them to the provider's own
    popup, where they are visible anyway. The secret half of the handshake
    — the app secret that trades the popup's code for a token — never
    leaves the backend. Served from here rather than baked into each
    frontend build so one env change reaches every console.

    ``configured`` is False when the deployment lacks either id; the
    console then says signup is not set up instead of opening a popup that
    cannot work.
    """

    connector_key: str
    configured: bool
    app_id: Optional[str] = None
    config_id: Optional[str] = None
    graph_version: str


class SubscriptionResult(BaseModel):
    """What was resubscribed. The provider's account id is echoed so an
    operator running the recovery across several accounts can see which one
    answered."""

    installation_id: str
    external_account_id: str
    subscribed: bool = True


# ---------------------------------------------------------------------------
# Numbers — the one templates go out from (is_primary) and the one Buddy
# answers on, which holds Buddy's settings under
# crm_channel_binding.capabilities["conversation"] (inbox R1, D13–D15, D24–D28)
# ---------------------------------------------------------------------------

#: The words sent when Buddy's settings do not set their own.
DEFAULT_CLOSING_MESSAGE = (
    "We're closing this chat as we haven't heard from you. "
    "Reply with any message to start again."
)
DEFAULT_NON_TEXT_MESSAGE = (
    "Sorry, I can only read text messages right now. Please type your question."
)
DEFAULT_OUT_OF_CREDITS_MESSAGE = (
    "We can't reply here right now. Please try again a little later."
)

#: Bounds on the two timings, so a typo cannot close every chat at once or
#: leave a customer waiting a day for a teammate.
CLOSING_LEAD_MINUTES_RANGE = (1, 120)
CLAIM_SLA_MINUTES_RANGE = (1, 240)
#: Longest custom message — Meta's plain-text ceiling is 4096; these are
#: one-line notices.
SETTINGS_MESSAGE_MAX = 1000


class ConversationSettings(BaseModel):
    """Buddy's settings, on Buddy's number. Every field has a working
    default, so settings never saved behave: human handoff OFF (D15, fail
    closed — missing is off), no agent (her message waits in the Inbox,
    R2), the standard closing / non-text / out-of-credits words.
    """

    #: Whether a person may take a conversation on Buddy's number (D15). Off
    #: means Buddy cannot hand off and the Inbox is read-only.
    human_handoff: bool = False
    #: The chat agent (template id) that answers on Buddy's number when
    #: nobody holds the thread (R2). None = no automatic answer.
    default_agent_id: Optional[str] = None
    closing_message: str = Field(
        DEFAULT_CLOSING_MESSAGE, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )
    closing_lead_minutes: int = Field(
        15, ge=CLOSING_LEAD_MINUTES_RANGE[0], le=CLOSING_LEAD_MINUTES_RANGE[1]
    )
    claim_sla_minutes: int = Field(
        10, ge=CLAIM_SLA_MINUTES_RANGE[0], le=CLAIM_SLA_MINUTES_RANGE[1]
    )
    non_text_message: str = Field(
        DEFAULT_NON_TEXT_MESSAGE, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )
    out_of_credits_message: str = Field(
        DEFAULT_OUT_OF_CREDITS_MESSAGE, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )


class ChannelSettingsRead(BaseModel):
    """One number as the console's Numbers tab shows it."""

    binding_id: str
    channel: str
    address: str
    #: The account (installation) it belongs to — a WhatsApp Business
    #: Account. The primary only moves within one (D27).
    installation_id: str
    #: Whether templates go out from it.
    is_primary: bool
    status: str
    #: Whether Buddy answers on it — the one number holding Buddy's settings.
    is_buddy_number: bool
    #: Buddy's settings; None on every other number.
    conversation: Optional[ConversationSettings] = None


class ConversationSettingsPatch(TenantScoped):
    """PATCH body for one number; only the fields sent change.

    ``is_primary: true`` makes templates go out from it (same account only,
    D27). ``is_buddy_number: true`` — or any of Buddy's fields below — moves
    Buddy's settings to it first, whole (D25). Neither takes ``false``: pick
    another number instead. ``null`` on one of Buddy's fields restores its
    default (for ``default_agent_id``: no automatic answer).
    """

    is_primary: Optional[Literal[True]] = None
    is_buddy_number: Optional[Literal[True]] = None
    human_handoff: Optional[bool] = None
    default_agent_id: Optional[str] = Field(None, min_length=1, max_length=64)
    closing_message: Optional[str] = Field(
        None, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )
    closing_lead_minutes: Optional[int] = Field(
        None, ge=CLOSING_LEAD_MINUTES_RANGE[0], le=CLOSING_LEAD_MINUTES_RANGE[1]
    )
    claim_sla_minutes: Optional[int] = Field(
        None, ge=CLAIM_SLA_MINUTES_RANGE[0], le=CLAIM_SLA_MINUTES_RANGE[1]
    )
    non_text_message: Optional[str] = Field(
        None, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )
    out_of_credits_message: Optional[str] = Field(
        None, min_length=1, max_length=SETTINGS_MESSAGE_MAX
    )
