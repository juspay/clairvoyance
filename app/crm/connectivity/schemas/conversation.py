"""Conversation shapes: Buddy's settings on the binding Buddy answers on
(``crm_channel_binding.capabilities["conversation"]``), and the binding
settings the console reads and patches — which binding templates go out
from, which one Buddy answers on, and Buddy's settings (inbox R1, D13–D15,
D24–D28)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.crm.connectivity.schemas.tenancy import TenantScoped

#: The words sent when Buddy's settings do not set their own.
DEFAULT_CLOSING_MESSAGE = (
    "We're closing this chat as we haven't heard from you. "
    "Reply with any message to start again."
)
DEFAULT_NON_TEXT_MESSAGE = (
    "Sorry, I can only read text messages right now. Please type your question."
)

#: Bounds on the two timings, so a typo cannot close every chat at once or
#: leave a customer waiting a day for a teammate.
CLOSING_LEAD_MINUTES_RANGE = (1, 120)
CLAIM_SLA_MINUTES_RANGE = (1, 240)
#: Longest custom message — well under every channel's ``max_text_chars``; these
#: are one-line notices.
SETTINGS_MESSAGE_MAX = 1000


class ConversationSettings(BaseModel):
    """Buddy's settings, on Buddy's binding. Every field has a working
    default, so settings never saved behave: human handoff OFF (D15, fail
    closed — missing is off), no agent (her message waits in the Inbox,
    R2), the standard closing and non-text words.
    """

    #: Whether a person may take a conversation on Buddy's binding (D15). Off
    #: means Buddy cannot hand off and the Inbox is read-only.
    human_handoff: bool = False
    #: The chat agent (template id) that answers on Buddy's binding when
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


class ChannelSettingsRead(BaseModel):
    """One binding with its template and Buddy roles, as the console lists
    it."""

    binding_id: str
    channel: str
    address: str
    #: The provider account (installation) it belongs to — a WhatsApp
    #: Business Account on WhatsApp. The primary only moves within one (D27).
    installation_id: str
    #: Whether templates go out from it.
    is_primary: bool
    status: str
    #: Whether Buddy answers on it — the one binding holding Buddy's settings.
    is_buddy_binding: bool
    #: Buddy's settings; None on every other binding.
    conversation: Optional[ConversationSettings] = None


class ConversationSettingsPatch(TenantScoped):
    """PATCH body for one binding; only the fields sent change.

    ``is_primary: true`` makes templates go out from it (same account only,
    D27). ``is_buddy_binding: true`` — or any of Buddy's fields below — moves
    Buddy's settings to it first, whole (D25). Neither takes ``false``: pick
    another binding instead. ``null`` on one of Buddy's fields restores its
    default (for ``default_agent_id``: no automatic answer).
    """

    is_primary: Optional[Literal[True]] = None
    is_buddy_binding: Optional[Literal[True]] = None
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
