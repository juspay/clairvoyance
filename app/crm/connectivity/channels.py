"""What the platform knows about each channel, adapter-independent.

One registry, one entry per channel the CRM can speak. The metadata here is
everything OUTSIDE the send door that varies by channel — which handle kind
the permission gate probes, whether the channel pre-registers message
templates, and whether (and how) it carries a free-form conversation; W8's
pacing and quality-tier defaults join as fields on Channel, not as new dicts
scattered per file.

Every per-channel question generic code asks is answered HERE, and only
here. The scar: queue.py once carried its own tuple of "phone channels" and
send.py assumed every channel registers templates — two parallel answers to
questions this registry already owned, each one a channel added in one place
and forgotten in the other.

Why this file and not providers/__init__: rule 11 confines providers/
behind send.py, so anything dispatch (or later, pacing) needs per channel
must live outside the confined package. The two registries are pinned
against drift in the test suite instead: every adapter channel has an entry
HERE (ADAPTERS ⊆ CHANNELS), and a channel without one fails closed at the
gate.

shared/redact.py's mask branch stays where it is on purpose: its default is
mask-everything, so an unregistered channel already fails safe — and
folding it here would make shared/ import connectivity.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ConversationProfile:
    """How a channel carries a free-form conversation — the facts a reply
    has to fit before it is written, and the window it has to fit inside.

    Read by the send door (a body past a limit is refused before the wire)
    and, through contracts, by whoever COMPOSES a reply (the conversations
    module, Buddy's reply formatter) so a reply is shaped to fit rather
    than refused. The limits are the provider's published ones; a reply that
    exceeds them would be refused by the provider with a code instead.
    """

    #: Hours a customer's message keeps the free-form window open. The
    #: window itself is always a predicate (last customer message + this >
    #: now) — never stored.
    window_hours: int
    #: Longest plain text body.
    text_max: int
    #: Reply buttons on one message, and the longest button title.
    max_reply_buttons: int
    reply_button_title_max: int
    #: Rows on one list message, and the longest row title / description.
    max_list_rows: int
    list_row_title_max: int
    list_row_description_max: int
    #: The body of an interactive message, a header, a footer, the label on
    #: a list's open button, and an image caption.
    interactive_body_max: int
    header_max: int
    footer_max: int
    list_button_max: int
    caption_max: int


@dataclass(frozen=True)
class Channel:
    """One channel's adapter-independent metadata."""

    # The platform_identity handle kind the gate probes for suppression —
    # a suppressed value on this kind of handle is what "STOP" wrote. It is
    # also the address kind the manifest stores for this channel, which is why
    # queue.py normalizes by it.
    gate_handle_kind: str
    # Whether sends on this channel must name a template APPROVED in the T23
    # registry (ADR 0011). WhatsApp and SMS-DLT do; email does not — for a
    # channel that does not, the send door skips the registry and the route
    # carries no template row.
    registers_templates: bool
    # How the channel carries a free-form conversation, or None for a
    # channel that cannot (a template-only pipe). None refuses every session
    # send on the channel, and the conversations module offers no inbox for
    # it — fail closed, the same posture as the gate on an unknown channel.
    conversation: Optional[ConversationProfile] = None


#: Meta's published Cloud API limits (text 4096; interactive body 1024;
#: 3 reply buttons of 20 characters; 10 list rows of 24 / 72; header 60;
#: footer 60; list button 20; image caption 1024) and its 24-hour
#: customer-service window, reset by every message the customer sends.
WHATSAPP_CONVERSATION = ConversationProfile(
    window_hours=24,
    text_max=4096,
    max_reply_buttons=3,
    reply_button_title_max=20,
    max_list_rows=10,
    list_row_title_max=24,
    list_row_description_max=72,
    interactive_body_max=1024,
    header_max=60,
    footer_max=60,
    list_button_max=20,
    caption_max=1024,
)

CHANNELS: Dict[str, Channel] = {
    "whatsapp": Channel(
        gate_handle_kind="phone",
        registers_templates=True,
        conversation=WHATSAPP_CONVERSATION,
    ),
}


def gate_handle_kind_for(channel: str) -> Optional[str]:
    """The handle kind the gate probes for ``channel``.

    None means unregistered, and the gate fails CLOSED on it
    (dispatch.gate) — a channel this registry cannot describe must not
    slip past the one check a person who said STOP is protected by.
    """
    entry = CHANNELS.get(channel)
    return entry.gate_handle_kind if entry else None


def registers_templates_for(channel: str) -> bool:
    """Whether sends on ``channel`` must name an approved registry template.

    An unregistered channel answers True: the send door then looks the
    template up and refuses — fail CLOSED, the same posture the gate takes on
    a channel this registry cannot describe.
    """
    entry = CHANNELS.get(channel)
    return entry.registers_templates if entry else True


def conversation_channels() -> Tuple[str, ...]:
    """Every channel that carries a free-form conversation — the ones the
    inbox keeps threads for, the closing sweep watches and Buddy answers on.
    Generic code iterates these; it never names a channel itself."""
    return tuple(
        name for name, entry in CHANNELS.items() if entry.conversation is not None
    )


def conversation_profile(channel: str) -> Optional[ConversationProfile]:
    """How ``channel`` carries a free-form conversation, or None when it
    cannot — an unregistered channel included. Callers refuse on None."""
    entry = CHANNELS.get(channel)
    return entry.conversation if entry else None
