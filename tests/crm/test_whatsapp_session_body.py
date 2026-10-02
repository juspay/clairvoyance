"""Free-form replies on WhatsApp: the channel-neutral body shapes, what the
Cloud API body looks like for each, the channel's limits checked before the
wire, and the adapter's conversation face end to end (no network — the
same MockTransport stand-in the template tests use)."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from app.crm.connectivity.channels import (
    WHATSAPP_CONVERSATION,
    conversation_profile,
)
from app.crm.connectivity.providers import ADAPTERS
from app.crm.connectivity.providers.base import ChannelAdapter
from app.crm.connectivity.providers.whatsapp import adapter as whatsapp_module
from app.crm.connectivity.providers.whatsapp.adapter import MetaWhatsAppAdapter
from app.crm.connectivity.providers.whatsapp.payload import (
    build_session_body,
    session_body_problem,
)
from app.crm.connectivity.schemas.connector import ChannelBinding, ConnectorInstallation
from app.crm.connectivity.schemas.message import (
    ButtonsBody,
    CredentialBundle,
    ImageBody,
    ListBody,
    ListRow,
    QueuedMessage,
    ReplyButton,
    SendOutcome,
    SendRoute,
    SessionBody,
    TextBody,
)
from tests.crm.doubles import stub_http

ACCEPTED = {"messages": [{"id": "wamid.REPLY"}]}


def _message(**overrides) -> QueuedMessage:
    fields = dict(
        id="m-1",
        merchant_id="shop",
        customer_id="c-1",
        channel="whatsapp",
        sent_to_address="+919876543210",
        source_kind="agent",
        purpose_key="service.conversation",
        template_id=None,
        variables={},
        dedupe_key="turn-1:0",
        attempt=1,
        next_attempt_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return QueuedMessage(**fields)


def _route(**bundle) -> SendRoute:
    return SendRoute(
        installation=ConnectorInstallation(
            id="i-1",
            merchant_id="shop",
            connector_key="whatsapp",
            external_account_id="waba-1",
            credential_id="cred-1",
            status="healthy",
        ),
        binding=ChannelBinding(
            id="b-1",
            merchant_id="shop",
            channel="whatsapp",
            installation_id="i-1",
            address="PHONE_NUMBER_ID",
            is_primary=True,
            status="active",
        ),
        bundle=CredentialBundle(values=bundle or {"system_user_token": "tok"}),
    )


def _buttons(n: int, title: str = "Yes") -> ButtonsBody:
    return ButtonsBody(
        text="Pick one",
        buttons=[ReplyButton(id=f"b{i}", title=title) for i in range(n)],
    )


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


# --- the Cloud API body ------------------------------------------------------


def test_text_becomes_a_text_message() -> None:
    body = build_session_body("919876543210", TextBody(text="Hello Priya"))
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "text",
        "text": {"preview_url": False, "body": "Hello Priya"},
    }


def test_a_reply_quotes_the_message_it_answers() -> None:
    body = build_session_body(
        "919876543210", TextBody(text="Sure", reply_to="wamid.THEIRS")
    )
    assert body["context"] == {"message_id": "wamid.THEIRS"}


def test_buttons_become_reply_buttons_with_their_ids() -> None:
    body = build_session_body(
        "919876543210",
        ButtonsBody(
            text="Which size?",
            buttons=[ReplyButton(id="s8", title="8"), ReplyButton(id="s9", title="9")],
            header="Running shoe",
            footer="Tap one",
        ),
    )
    interactive = body["interactive"]
    assert body["type"] == "interactive" and interactive["type"] == "button"
    assert interactive["body"] == {"text": "Which size?"}
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "s8", "title": "8"}},
        {"type": "reply", "reply": {"id": "s9", "title": "9"}},
    ]
    assert interactive["header"] == {"type": "text", "text": "Running shoe"}
    assert interactive["footer"] == {"text": "Tap one"}


def test_a_list_becomes_one_section_of_rows() -> None:
    body = build_session_body(
        "919876543210",
        ListBody(
            text="Which order?",
            button="Choose",
            section_title="Recent",
            rows=[
                ListRow(id="o1", title="#4812", description="Shoes"),
                ListRow(id="o2", title="#4813"),
            ],
        ),
    )
    action = body["interactive"]["action"]
    assert body["interactive"]["type"] == "list"
    assert action["button"] == "Choose"
    assert action["sections"] == [
        {
            "title": "Recent",
            "rows": [
                {"id": "o1", "title": "#4812", "description": "Shoes"},
                {"id": "o2", "title": "#4813"},
            ],
        }
    ]


def test_an_image_is_sent_by_link_with_its_caption() -> None:
    body = build_session_body(
        "919876543210", ImageBody(url="https://cdn.shop.in/a.jpg", caption="This one")
    )
    assert body["type"] == "image"
    assert body["image"] == {"link": "https://cdn.shop.in/a.jpg", "caption": "This one"}


# --- the channel's limits, before the wire ------------------------------------


def test_whatsapp_carries_a_24_hour_conversation() -> None:
    profile = conversation_profile("whatsapp")
    assert profile is WHATSAPP_CONVERSATION
    assert profile.window_hours == 24
    assert conversation_profile("carrier_pigeon") is None


@pytest.mark.parametrize(
    ("body", "fits"),
    [
        (_buttons(3), True),
        (_buttons(4), False),
        (_buttons(1, title="x" * 20), True),
        (_buttons(1, title="x" * 21), False),
        (TextBody(text="x" * 4096), True),
        (TextBody(text="x" * 4097), False),
        (ButtonsBody(text="x" * 1025, buttons=[ReplyButton(id="a", title="A")]), False),
        (
            ListBody(
                text="Pick",
                button="Open",
                rows=[ListRow(id=str(i), title="Row") for i in range(10)],
            ),
            True,
        ),
        (
            ListBody(
                text="Pick",
                button="Open",
                rows=[ListRow(id=str(i), title="Row") for i in range(11)],
            ),
            False,
        ),
        (
            ListBody(
                text="Pick", button="Open", rows=[ListRow(id="r", title="x" * 25)]
            ),
            False,
        ),
        (ImageBody(url="https://a.in/x.jpg", caption="x" * 1025), False),
    ],
)
def test_a_reply_past_the_channels_limits_is_named_before_the_wire(body, fits) -> None:
    problem = session_body_problem(body, WHATSAPP_CONVERSATION)
    assert (problem is None) is fits
    if problem is not None:
        # The problem names the part, never the customer-facing words.
        assert "x" * 20 not in problem


# --- the adapter's conversation face -----------------------------------------


async def test_a_reply_is_posted_to_the_numbers_messages_endpoint(monkeypatch) -> None:
    seen = stub_http(
        monkeypatch, whatsapp_module, lambda r: httpx.Response(200, json=ACCEPTED)
    )
    outcome = await MetaWhatsAppAdapter().deliver_session(
        _message(), _route(), TextBody(text="Your order ships tomorrow")
    )
    assert outcome == SendOutcome(status="accepted", provider_message_id="wamid.REPLY")
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path.endswith("/PHONE_NUMBER_ID/messages")
    assert request.headers["authorization"] == "Bearer tok"
    sent = json.loads(request.content)
    assert sent["to"] == "919876543210" and sent["type"] == "text"


@pytest.mark.parametrize(
    ("message", "route", "body", "reason"),
    [
        (
            _message(),
            _route(other="x"),
            TextBody(text="Hi"),
            "connector_credential_missing",
        ),
        (
            _message(sent_to_address="12"),
            _route(),
            TextBody(text="Hi"),
            "recipient_address_invalid",
        ),
        (_message(), _route(), _buttons(4), "message_body_invalid"),
    ],
)
async def test_every_refusal_before_the_wire_is_ours_and_posts_nothing(
    monkeypatch, message, route, body, reason
) -> None:
    seen = stub_http(monkeypatch, whatsapp_module, lambda r: httpx.Response(200))
    outcome = await MetaWhatsAppAdapter().deliver_session(message, route, body)
    assert outcome.status == "blocked" and outcome.reason == reason
    assert seen == []


async def test_a_closed_window_is_the_providers_terminal_no(monkeypatch) -> None:
    """131047: more than 24 hours since she last wrote. Not retryable —
    waiting never reopens a window; only her next message does."""
    stub_http(
        monkeypatch,
        whatsapp_module,
        lambda r: httpx.Response(400, json={"error": {"code": 131047}}),
    )
    outcome = await MetaWhatsAppAdapter().deliver_session(
        _message(), _route(), TextBody(text="Still there?")
    )
    assert outcome.status == "failed"
    assert outcome.reason == "131047" and outcome.retryable is False


async def test_an_adapter_without_a_conversation_face_refuses() -> None:
    """The port's default: a channel that has not learned free-form replies
    says so on the row instead of guessing a shape."""

    class _TemplateOnly(ChannelAdapter):
        channel = "sms"

        async def deliver(self, message, route):
            raise AssertionError("not reached")

    outcome = await _TemplateOnly().deliver_session(
        _message(), _route(), TextBody(text="Hi")
    )
    assert outcome.status == "blocked"
    assert outcome.reason == "channel_cannot_send_free_form"


def test_every_channel_with_a_conversation_has_an_adapter_that_carries_one() -> None:
    """A CHANNELS entry claiming a conversation, served by an adapter still
    on the port's refusing default, would offer an inbox whose replies all
    come back blocked."""
    for channel, adapter in ADAPTERS.items():
        if conversation_profile(channel) is not None:
            assert (
                type(adapter).deliver_session is not ChannelAdapter.deliver_session
            ), f"{channel}: conversation declared, deliver_session not implemented"
