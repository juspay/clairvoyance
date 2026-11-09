"""What the platform knows about each channel, adapter-independent.

One registry, one entry per channel the CRM can speak. The metadata here is
everything OUTSIDE the send door that varies by channel — which handle kind
the permission gate probes, and whether the channel pre-registers message
templates; W8's pacing and quality-tier defaults join as fields on Channel,
not as new dicts scattered per file.

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
from typing import Dict, Optional


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
    # Whether a send may carry a LIST as a variable, not only one line of
    # text. A WhatsApp blank is one line, so a declared list reaches it
    # joined; a Xyne form's options are the elements themselves ("$slots"
    # fills from data.slots), read off the run's letter at send time.
    structured_variables: bool = False


CHANNELS: Dict[str, Channel] = {
    "whatsapp": Channel(gate_handle_kind="phone", registers_templates=True),
    # A Xyne form goes to a person's Xyne user id (a crm_customer_handle
    # row, Xyne 2/5). Its layout is registered with US, not with Xyne —
    # Xyne keeps no templates; every message carries its whole form — and
    # Xyne's own validator checks it once, on submit (xyne/templates.py).
    "xyne": Channel(
        gate_handle_kind="xyne_user_id",
        registers_templates=True,
        structured_variables=True,
    ),
}


def gate_handle_kind_for(channel: str) -> Optional[str]:
    """The handle kind the gate probes for ``channel``.

    None means unregistered, and the gate fails CLOSED on it
    (gate.gate()) — a channel this registry cannot describe must not
    slip past the one check a person who said STOP is protected by.
    """
    entry = CHANNELS.get(channel)
    return entry.gate_handle_kind if entry else None


def takes_structured_variables(channel: str) -> bool:
    """Whether a send on ``channel`` may carry a list as a variable. An
    unregistered channel answers False — the text-only posture every
    channel had before this flag."""
    entry = CHANNELS.get(channel)
    return entry.structured_variables if entry else False


def registers_templates_for(channel: str) -> bool:
    """Whether sends on ``channel`` must name an approved registry template.

    An unregistered channel answers True: the send door then looks the
    template up and refuses — fail CLOSED, the same posture the gate takes on
    a channel this registry cannot describe.
    """
    entry = CHANNELS.get(channel)
    return entry.registers_templates if entry else True
