"""What the platform knows about each channel, adapter-independent.

One registry, one entry per channel the CRM can speak. The metadata here is
everything OUTSIDE the send door that varies by channel — which handle kind
the permission gate probes, whether the channel pre-registers message
templates, and whether (and how) it carries a free-form conversation; W8's
pacing and quality-tier defaults join as fields on Channel, not as new dicts
scattered per file.

Every per-channel question generic code asks is answered from HERE, and
only here (the lookups that read it live in utils.py). The scar: queue.py once carried its own tuple of "phone channels" and
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
from typing import Dict, Optional


@dataclass(frozen=True)
class ConversationLimits:
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
    max_text_chars: int
    #: Reply buttons on one message, and the longest button title.
    max_reply_buttons: int
    max_reply_button_title_chars: int
    #: Rows on one list message, and the longest row title / description.
    max_list_rows: int
    max_list_row_title_chars: int
    max_list_row_description_chars: int
    #: The body of an interactive message, a header, a footer, the label on
    #: a list's open button, and an image caption.
    max_interactive_body_chars: int
    max_header_chars: int
    max_footer_chars: int
    max_list_button_label_chars: int
    max_caption_chars: int


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
    conversation_limits: Optional[ConversationLimits] = None


#: Meta's published Cloud API limits (text 4096; interactive body 1024;
#: 3 reply buttons of 20 characters; 10 list rows of 24 / 72; header 60;
#: footer 60; list button 20; image caption 1024) and its 24-hour
#: customer-service window, reset by every message the customer sends.
WHATSAPP_CONVERSATION_LIMITS = ConversationLimits(
    window_hours=24,
    max_text_chars=4096,
    max_reply_buttons=3,
    max_reply_button_title_chars=20,
    max_list_rows=10,
    max_list_row_title_chars=24,
    max_list_row_description_chars=72,
    max_interactive_body_chars=1024,
    max_header_chars=60,
    max_footer_chars=60,
    max_list_button_label_chars=20,
    max_caption_chars=1024,
)

CHANNELS: Dict[str, Channel] = {
    "whatsapp": Channel(
        gate_handle_kind="phone",
        registers_templates=True,
        conversation_limits=WHATSAPP_CONVERSATION_LIMITS,
    ),
}
