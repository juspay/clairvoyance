"""Meta's inbound face: their signature, their handshake, their envelope.

The face's own half only — verify, challenge, and the walk from Meta's
entry[]/changes[]/value to letters that still name a provider OWNER. Who
that owner belongs to is connectivity/ingress.py's business, tested in
test_connectivity_ingress.py; the record door that mounts it is tested in
test_ingress_door.py.
"""

import hashlib
import hmac

import pytest

from app.crm.connectivity.providers.meta import inbound
from app.crm.connectivity.topics import (
    TOPIC_ACCOUNT,
    TOPIC_INBOUND,
    TOPIC_STATUS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)

SECRET = "meta-app-secret-for-tests"
TOKEN = "meta-verify-token-for-tests"
NUMBER = "812345678901234"
WABA = "waba-77"
OUT_WAMID = "wamid.OUT"
IN_WAMID = "wamid.IN"
TS = "1788177600"


@pytest.fixture
def secrets(monkeypatch):
    """Both platform secrets present — the normal running state."""
    monkeypatch.setattr(inbound, "META_APP_SECRET", SECRET)
    monkeypatch.setattr(inbound, "META_WEBHOOK_VERIFY_TOKEN", TOKEN)


def _sig(raw: bytes, secret: str = SECRET) -> str:
    """A valid X-Hub-Signature-256 over these exact bytes."""
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _status(**overrides) -> dict:
    """A delivery receipt item as Meta sends one."""
    fields = {"id": OUT_WAMID, "status": "delivered", "timestamp": TS}
    fields.update(overrides)
    return fields


def _message(**overrides) -> dict:
    """An inbound customer message as Meta sends one."""
    fields = {
        "from": "919876543210",
        "id": IN_WAMID,
        "timestamp": TS,
        "type": "text",
        "text": {"body": "yes"},
    }
    fields.update(overrides)
    return fields


def _value(number=NUMBER, **overrides) -> dict:
    """One "messages" value, addressed to one of our numbers."""
    fields = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": number}}
    fields.update(overrides)
    return fields


def _body(*changes, obj="whatsapp_business_account", waba=WABA) -> dict:
    """Meta's envelope. Each change is (field, value)."""
    return {
        "object": obj,
        "entry": [
            {
                "id": waba,
                "time": int(TS),
                "changes": [{"field": f, "value": v} for f, v in changes],
            }
        ],
    }


# --- their signature ----------------------------------------------------------


def test_a_correct_signature_over_these_exact_bytes_passes(secrets) -> None:
    """A correct signature over these exact bytes passes."""
    raw = b'{"object": "whatsapp_business_account"}'
    assert inbound.verify_signature(raw, {"X-Hub-Signature-256": _sig(raw)})


def test_the_header_is_matched_whatever_its_casing(secrets) -> None:
    """The header is matched whatever its casing."""
    raw = b"{}"
    assert inbound.verify_signature(raw, {"x-hub-signature-256": _sig(raw)})


def test_a_signature_for_different_bytes_is_refused(secrets) -> None:
    """A signature for different bytes is refused."""
    assert not inbound.verify_signature(
        b"tampered", {"X-Hub-Signature-256": _sig(b"real")}
    )


def test_a_signature_from_a_different_secret_is_refused(secrets) -> None:
    """A signature from a different secret is refused."""
    raw = b"{}"
    header = {"X-Hub-Signature-256": _sig(raw, "not-the-secret")}
    assert not inbound.verify_signature(raw, header)


@pytest.mark.parametrize(
    "header", [{}, {"X-Hub-Signature-256": ""}, {"X-Hub-Signature-256": "md5=abc"}]
)
def test_a_missing_or_misshapen_signature_is_refused(secrets, header) -> None:
    """A missing or misshapen signature is refused."""
    assert not inbound.verify_signature(b"{}", header)


def test_without_a_configured_secret_everything_is_refused(monkeypatch) -> None:
    """Without a configured secret everything is refused."""
    monkeypatch.setattr(inbound, "META_APP_SECRET", "")
    raw = b"{}"
    assert not inbound.verify_signature(raw, {"X-Hub-Signature-256": _sig(raw)})


# --- their handshake ----------------------------------------------------------


def test_the_handshake_echoes_the_challenge_for_our_token(secrets) -> None:
    """The handshake echoes the challenge for our token."""
    params = {"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "9"}
    assert inbound.handshake_challenge(params) == "9"


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "9"},
        {"hub.mode": "unsubscribe", "hub.verify_token": TOKEN, "hub.challenge": "9"},
        {"hub.mode": "subscribe", "hub.challenge": "9"},
        {},
    ],
)
def test_the_handshake_refuses_anything_else(secrets, params) -> None:
    """The handshake refuses anything else."""
    assert inbound.handshake_challenge(params) is None


def test_without_a_configured_token_no_handshake_succeeds(monkeypatch) -> None:
    """Without a configured token no handshake succeeds."""
    monkeypatch.setattr(inbound, "META_WEBHOOK_VERIFY_TOKEN", "")
    params = {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "9"}
    assert inbound.handshake_challenge(params) is None


# --- their envelope -> letters ------------------------------------------------


def test_a_status_becomes_one_letter_per_transition() -> None:
    """A status becomes one letter per transition."""
    body = _body(
        ("messages", _value(statuses=[_status(), _status(status="read")])),
    )
    out = inbound.letters(body)
    assert [(l.topic, l.external_id) for l in out] == [
        (TOPIC_STATUS, f"{OUT_WAMID}:delivered"),
        (TOPIC_STATUS, f"{OUT_WAMID}:read"),
    ]
    assert all(
        l.owner_kind == inbound.OWNER_ENDPOINT and l.owner_id == NUMBER for l in out
    )


def test_an_inbound_message_is_keyed_by_its_own_id() -> None:
    """An inbound message is keyed by its own id."""
    out = inbound.letters(_body(("messages", _value(messages=[_message()]))))
    assert [(l.topic, l.external_id) for l in out] == [(TOPIC_INBOUND, IN_WAMID)]


def test_every_letter_carries_its_source_from_the_body_object() -> None:
    """Every letter carries its source from the body's object."""
    out = inbound.letters(_body(("messages", _value(statuses=[_status()]))))
    assert out[0].source == "whatsapp"


def test_an_object_we_do_not_serve_yields_nothing() -> None:
    # A guessed source would file letters no consumer reads.
    """An object we do not serve yields nothing."""
    body = _body(("messages", _value(statuses=[_status()])), obj="page_mystery")
    assert inbound.letters(body) == []


def test_the_payload_is_metas_value_narrowed_to_one_item() -> None:
    # Canon T13 col 7: the letter they sent, never our understanding of it —
    # a recorded callback is a valid fixture unchanged.
    """The payload is Meta's value narrowed to one item."""
    contacts = [{"wa_id": "919876543210", "profile": {"name": "Priya"}}]
    value = _value(messages=[_message()], contacts=contacts)
    out = inbound.letters(_body(("messages", value)))
    payload = out[0].payload
    assert payload["messages"] == [_message()]
    assert payload["contacts"] == contacts
    assert payload["metadata"] == {"phone_number_id": NUMBER}
    assert payload["messaging_product"] == "whatsapp"
    assert "statuses" not in payload


def test_a_value_with_no_receiving_number_yields_no_message_letters() -> None:
    """A value with no receiving number yields no message letters."""
    value = {"statuses": [_status()]}
    assert inbound.letters(_body(("messages", value))) == []


def test_a_template_status_update_is_owned_by_the_waba() -> None:
    # Finding 2: these carry no phone_number_id — the WABA in entry.id is
    # the owner, and the external_id is COMPOSED (Meta sends none).
    """A template status update is owned by the WABA."""
    value = {
        "event": "APPROVED",
        "message_template_id": "tmpl-9",
        "message_template_name": "order_update",
        "message_template_language": "en",
    }
    out = inbound.letters(_body(("message_template_status_update", value)))
    assert len(out) == 1
    letter = out[0]
    assert letter.topic == TOPIC_TEMPLATE_STATUS
    assert letter.owner_kind == inbound.OWNER_ACCOUNT and letter.owner_id == WABA
    assert letter.external_id == f"{WABA}:tmpl-9:APPROVED:{TS}"
    assert letter.payload == value


def test_a_category_update_composes_its_own_event_word() -> None:
    """A category update composes its own event word."""
    value = {"message_template_id": "tmpl-9", "new_category": "MARKETING"}
    out = inbound.letters(_body(("template_category_update", value)))
    assert out[0].topic == TOPIC_TEMPLATE_CATEGORY
    assert out[0].external_id == f"{WABA}:tmpl-9:category:{TS}"


def test_a_quality_update_composes_its_own_event_word() -> None:
    """A quality update composes its own event word."""
    value = {"message_template_id": "tmpl-9", "new_quality_score": "GREEN"}
    out = inbound.letters(_body(("message_template_quality_update", value)))
    assert out[0].topic == TOPIC_TEMPLATE_QUALITY
    assert out[0].external_id == f"{WABA}:tmpl-9:quality:{TS}"


def test_an_account_update_is_a_letter_too() -> None:
    """An account update is a letter too."""
    value = {"event": "DISABLED_UPDATE"}
    out = inbound.letters(_body(("account_update", value)))
    assert out[0].topic == TOPIC_ACCOUNT
    assert out[0].external_id == f"{WABA}:account:DISABLED_UPDATE:{TS}"


def test_both_kinds_ride_one_callback() -> None:
    """Both kinds ride one callback."""
    out = inbound.letters(
        _body(
            ("messages", _value(statuses=[_status()])),
            (
                "message_template_status_update",
                {"event": "REJECTED", "message_template_id": "t"},
            ),
        )
    )
    assert {l.owner_kind for l in out} == {
        inbound.OWNER_ENDPOINT,
        inbound.OWNER_ACCOUNT,
    }


def test_every_letter_carries_the_three_words_named_apart() -> None:
    # schemas.ingress.ProviderLetter: source is the spine's word, channel
    # the binding's, connector_key the installation's. They coincide for
    # Meta, so the face must still say all three — the root never assumes.
    """Every letter carries source, channel and connector_key, named apart."""
    out = inbound.letters(
        _body(
            ("messages", _value(statuses=[_status()])),
            ("message_template_status_update", {"event": "APPROVED"}),
        )
    )
    assert len(out) == 2
    for letter in out:
        assert (letter.source, letter.channel, letter.connector_key) == (
            "whatsapp",
            "whatsapp",
            "whatsapp",
        )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"object": "whatsapp_business_account"},
        {"object": "whatsapp_business_account", "entry": "nope"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": 3}]}]},
    ],
)
def test_an_unreadable_envelope_yields_nothing_and_raises_nothing(body) -> None:
    """An unreadable envelope yields nothing and raises nothing."""
    assert inbound.letters(body) == []


def test_a_malformed_entry_does_not_hide_a_good_one() -> None:
    """A malformed entry does not hide a good one."""
    body = _body(("messages", _value(statuses=[_status()])))
    body["entry"].insert(0, "garbage")
    assert len(inbound.letters(body)) == 1


def test_letters_carry_the_providers_clock() -> None:
    """Letters carry the provider's clock."""
    out = inbound.letters(_body(("messages", _value(statuses=[_status()]))))
    assert out[0].occurred_at is not None
    assert int(out[0].occurred_at.timestamp()) == int(TS)


def test_an_unusable_timestamp_never_raises() -> None:
    """An unusable timestamp never raises."""
    out = inbound.letters(
        _body(("messages", _value(statuses=[_status(timestamp="soon")])))
    )
    assert out[0].occurred_at is None


def test_a_button_tap_survives_the_narrowing_intact() -> None:
    # A workflow's Yes/No branching reads the derived `reply` field
    # (below) — pin that the nested shape underneath it still carries
    # both exactly as Meta sent them.
    """A button tap survives the narrowing intact."""
    tap = _message(
        type="button",
        button={"payload": "YES", "text": "Yes"},
        context={"id": "wamid.OUT1"},
    )
    out = inbound.letters(_body(("messages", _value(messages=[tap]))))
    message = out[0].payload["messages"][0]
    assert message["button"] == {"payload": "YES", "text": "Yes"}
    assert message["context"]["id"] == "wamid.OUT1"


def test_a_button_tap_flattens_onto_reply() -> None:
    """A template quick-reply's payload rides as a flat top-level `reply`,
    so a wait_event node's `key` can branch on it directly."""
    tap = _message(
        type="button", button={"payload": "CONFIRM_ORDER", "text": "Confirm"}
    )
    out = inbound.letters(_body(("messages", _value(messages=[tap]))))
    assert out[0].payload["reply"] == "CONFIRM_ORDER"


def test_an_interactive_button_reply_flattens_onto_reply() -> None:
    """A genuine Interactive button tap flattens the same way."""
    tap = _message(
        type="interactive",
        interactive={
            "type": "button_reply",
            "button_reply": {"id": "CANCEL_ORDER", "title": "Cancel"},
        },
    )
    out = inbound.letters(_body(("messages", _value(messages=[tap]))))
    assert out[0].payload["reply"] == "CANCEL_ORDER"


def test_an_interactive_list_reply_flattens_onto_reply() -> None:
    """A list-message reply flattens the same way, via list_reply.id."""
    tap = _message(
        type="interactive",
        interactive={
            "type": "list_reply",
            "list_reply": {"id": "OPTION_B", "title": "Option B"},
        },
    )
    out = inbound.letters(_body(("messages", _value(messages=[tap]))))
    assert out[0].payload["reply"] == "OPTION_B"


def test_a_text_message_carries_no_reply() -> None:
    """Free-text replies are out of scope for now — a plain text message
    yields no `reply` at all, same as before this field existed."""
    out = inbound.letters(_body(("messages", _value(messages=[_message()]))))
    assert "reply" not in out[0].payload
