"""Lookups on the channels registry: the questions generic code asks about a
channel. Each reads CHANNELS (registry.py); asked about a channel the
registry cannot describe, each fails CLOSED."""

from typing import Optional, Tuple

from app.crm.connectivity.channels.registry import CHANNELS, ConversationLimits


def get_gate_handle_kind(channel: str) -> Optional[str]:
    """The handle kind the gate probes for ``channel``.

    None means unregistered, and the gate fails CLOSED on it
    (gate.gate()) — a channel this registry cannot describe must not
    slip past the one check a person who said STOP is protected by.
    """
    entry = CHANNELS.get(channel)
    return entry.gate_handle_kind if entry else None


def get_registers_templates(channel: str) -> bool:
    """Whether sends on ``channel`` must name an approved registry template.

    An unregistered channel answers True: the send door then looks the
    template up and refuses — fail CLOSED, the same posture the gate takes on
    a channel this registry cannot describe.
    """
    entry = CHANNELS.get(channel)
    return entry.registers_templates if entry else True


def get_supported_channels_for_conversations() -> Tuple[str, ...]:
    """Every channel that carries a free-form conversation — the ones the
    inbox keeps threads for, the closing sweep watches and Buddy answers on.
    Generic code iterates these; it never names a channel itself."""
    return tuple(
        name
        for name, entry in CHANNELS.items()
        if entry.conversation_limits is not None
    )


def get_conversation_channel_limits(channel: str) -> Optional[ConversationLimits]:
    """How ``channel`` carries a free-form conversation, or None when it
    cannot — an unregistered channel included. Callers refuse on None."""
    entry = CHANNELS.get(channel)
    return entry.conversation_limits if entry else None
