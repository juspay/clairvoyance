"""Free-form replies on any channel: the channel-neutral body shapes, and
the registry's conversation face — which channels carry a conversation, and
that each one has an adapter able to send it."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.crm.connectivity.channels import (
    CHANNELS,
    conversation_channels,
    conversation_profile,
)
from app.crm.connectivity.providers import ADAPTERS
from app.crm.connectivity.providers.base import ChannelAdapter
from app.crm.connectivity.schemas.message import ButtonsBody, SessionBody

# --- the body shapes ---------------------------------------------------------


def test_a_body_is_parsed_by_its_kind() -> None:
    parsed = TypeAdapter(SessionBody).validate_python(
        {"kind": "buttons", "text": "Size?", "buttons": [{"id": "s8", "title": "8"}]}
    )
    assert isinstance(parsed, ButtonsBody)


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "text", "text": ""},
        {
            "kind": "buttons",
            "text": "Size?",
            "buttons": [{"id": "s", "title": "8"}, {"id": "s", "title": "9"}],
        },
        {
            "kind": "list",
            "text": "Pick",
            "button": "Open",
            "rows": [{"id": "r", "title": "A"}, {"id": "r", "title": "B"}],
        },
        {"kind": "image", "url": "http://shop.in/a.jpg"},
        {"kind": "carrier_pigeon", "text": "coo"},
    ],
)
def test_a_body_that_cannot_mean_one_thing_is_refused(raw) -> None:
    """Empty words, two buttons with one id (which did she tap?), an image a
    provider would fetch over plain http, an unknown kind."""
    with pytest.raises(ValidationError):
        TypeAdapter(SessionBody).validate_python(raw)


# --- the registry's conversation face ----------------------------------------


def test_the_conversation_channels_are_the_ones_with_a_profile() -> None:
    """Generic code iterates these instead of naming a channel: a channel
    joins the inbox, the closing sweep and Buddy by declaring a profile."""
    channels = conversation_channels()
    assert channels and set(channels) == {
        name for name, entry in CHANNELS.items() if entry.conversation is not None
    }
    assert all(conversation_profile(name) is not None for name in channels)


def test_every_channel_with_a_conversation_has_an_adapter_that_carries_one() -> None:
    """A CHANNELS entry claiming a conversation, served by an adapter still
    on the port's refusing default, would offer an inbox whose replies all
    come back blocked."""
    for channel, adapter in ADAPTERS.items():
        if conversation_profile(channel) is not None:
            assert (
                type(adapter).deliver_session is not ChannelAdapter.deliver_session
            ), f"{channel}: conversation declared, deliver_session not implemented"
