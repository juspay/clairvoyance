"""XyneSpaces' inbound face: the relay's signature and envelope.

The face's own half only — verify, and the parse from the relay's documented
flow_action submission to a letter that names the merchant. Who that merchant
is and whether they own a xyne door is connectivity/ingress.py's business,
tested beside it; the record door that mounts the bay is tested in
test_ingress_door.py. Mirrors test_meta_inbound.py's shape.
"""

import hashlib
import hmac

import pytest

from app.crm.connectivity.providers.xyne import inbound
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION

SECRET = "xyne-secret-for-tests"
MERCHANT = "flow-shop"
MESSAGE_ID = "e9be4e96-9e96-4c9e-b259-ee75ea8bb499"


@pytest.fixture
def secrets(monkeypatch):
    """The platform secret present — the normal running state."""
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_SECRET", SECRET)


def _mac(raw: bytes, secret: str = SECRET) -> str:
    """The raw HMAC-SHA256 hexdigest over these exact bytes."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _sig(raw: bytes, secret: str = SECRET) -> str:
    """A valid x-xyne-signature, PLAIN hex — Xyne's documented scheme."""
    return _mac(raw, secret)


def _sig_prefixed(raw: bytes, secret: str = SECRET) -> str:
    """A valid signature with the sha256= prefix some relays send."""
    return "sha256=" + _mac(raw, secret)


def _body(**overrides) -> dict:
    """The relay's documented flow_action submission: the merchant and the
    customer's phone come back from what WE sent in context.flowJSON.data, and
    the event id is context.messageId."""
    fields = {
        "actionId": "submit-interview-availability",
        "type": "submit",
        "values": {
            "selected_dates": ["2026-10-15"],
            "slots_2026-10-15": ["10:00"],
        },
        "context": {
            "flowJSON": {
                "version": "2.0",
                "screenId": "interview-availability",
                "data": {
                    "merchant_id": MERCHANT,
                    "customer_mobile_number": "919876543210",
                    "event": {"pm_name": "PM_name", "stage": "Hackathon"},
                },
            },
            "messageId": MESSAGE_ID,
            "conversationId": "ead0b5ff-32f9-460c-92d6-f52847257465",
            "userId": "cmo014zxi0dz1rkq60eouxllf",
        },
    }
    fields.update(overrides)
    return fields


# --- their signature ---------------------------------------------------------


def test_a_correct_signature_over_these_exact_bytes_passes(secrets) -> None:
    """A correct PLAIN-HEX signature over these exact bytes passes."""
    raw = b'{"actionId": "submit"}'
    assert inbound.verify_signature(raw, {"X-Xyne-Signature": _sig(raw)})


def test_the_header_is_matched_whatever_its_casing(secrets) -> None:
    """The header is matched whatever its casing."""
    raw = b"{}"
    assert inbound.verify_signature(raw, {"x-xyne-signature": _sig(raw)})


def test_a_prefixed_signature_is_also_accepted(secrets) -> None:
    """The sha256= prefix (some relays' spelling) is trimmed and accepted: the
    digest is the same bytes either way."""
    raw = b"{}"
    assert inbound.verify_signature(raw, {"x-xyne-signature": _sig_prefixed(raw)})


def test_a_signature_for_different_bytes_is_refused(secrets) -> None:
    """A signature for different bytes is refused."""
    assert not inbound.verify_signature(
        b"tampered", {"x-xyne-signature": _sig(b"real")}
    )


def test_a_signature_from_a_different_secret_is_refused(secrets) -> None:
    """A signature from a different secret is refused."""
    assert not inbound.verify_signature(
        b"{}", {"x-xyne-signature": _sig(b"{}", secret="wrong-secret")}
    )


def test_a_missing_or_non_hex_header_is_refused(secrets) -> None:
    """A missing header, or one that is not hex, is refused — the same answer as
    a wrong signature, never a raise."""
    assert not inbound.verify_signature(b"{}", {})
    assert not inbound.verify_signature(b"{}", {"x-xyne-signature": "not-hex!!"})
    assert not inbound.verify_signature(b"{}", {"x-xyne-signature": "sha256="})


def test_no_secret_configured_refuses_everything(monkeypatch) -> None:
    """Empty secret is a legal boot but refuses everything — fail closed."""
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_SECRET", "")
    raw = b"{}"
    assert not inbound.verify_signature(raw, {"x-xyne-signature": _sig(raw)})


# --- their envelope ----------------------------------------------------------


def test_a_flow_action_submission_yields_one_letter(secrets) -> None:
    """A documented submission yields one letter naming the merchant from what
    we sent, keyed by the messageId the run saved."""
    out = inbound.letters(_body())
    assert len(out) == 1
    letter = out[0]
    assert letter.owner_kind == inbound.OWNER_ACCOUNT
    assert letter.owner_id == MERCHANT
    assert letter.topic == TOPIC_FLOW_ACTION
    assert letter.external_id == MESSAGE_ID
    assert letter.payload["context"]["flowJSON"]["data"]["merchant_id"] == MERCHANT


def test_every_letter_carries_the_three_words_named_apart(secrets) -> None:
    """Every letter carries source, channel and connector_key, named apart."""
    out = inbound.letters(_body())
    assert len(out) == 1
    letter = out[0]
    # They coincide for XyneSpaces, but the face must still say all three —
    # the ingress root never assumes (schemas.ingress.ProviderLetter).
    assert (letter.source, letter.channel, letter.connector_key) == (
        "xyne",
        "xyne",
        "xyne",
    )


def test_the_letter_carries_no_provider_clock(secrets) -> None:
    """Xyne sends no timestamp, so the letter's clock is the time received
    (record stamps received_at at the door); occurred_at stays None."""
    out = inbound.letters(_body())
    assert out[0].occurred_at is None


def test_the_whole_body_is_the_payload_verbatim(secrets) -> None:
    """The WHOLE signed body is the payload verbatim (canon T13 col 7) — the
    merchant and message id are read, not redacted or annotated."""
    out = inbound.letters(_body())
    assert out[0].payload == _body()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"context": "nope"},
        {"context": {"flowJSON": {"data": {"merchant_id": MERCHANT}}}},
        {"context": {"flowJSON": {"data": {}}, "messageId": MESSAGE_ID}},
        {"context": {"messageId": MESSAGE_ID}},
        {"messageId": MESSAGE_ID, "context": {"flowJSON": {"data": {}}}},
    ],
)
def test_an_unattributable_envelope_yields_nothing_and_raises_nothing(
    secrets, body
) -> None:
    """A callback without both the merchant I sent and a message id yields
    nothing and raises nothing — dropped, never guessed onto the spine."""
    assert inbound.letters(body) == []


def test_the_claimed_flat_reply_yields_nothing_and_raises_nothing(secrets) -> None:
    """A flat `MESSAGE_POSTED`-style body — `eventType`/`messageId` and a
    TOP-LEVEL `conversationId`, but NO `context.flowJSON` (the only trusted
    owner source) — files no letter.

    test_plan_templates.py bridges this body straight to the goal key, so
    __this__ face is pinned separately: `letters()` must NOT pick an owner or a
    customer out of an untrusted thread id. There is no documented reply
    contract in-tree (finding 7 is unresolved), so the correct behaviour today
    is the same as for any unattributable envelope: dropped, never guessed. A
    future PR that teaches the face a real reply must first obtain a captured
    Xyne body and replace this shape with the verified one.
    """
    reply = {
        "eventType": "MESSAGE_POSTED",
        "messageId": "m_new",
        "conversationId": "ead0b5ff-32f9-460c-92d6-f52847257465",
    }
    assert inbound.letters(reply) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "click"),
        ("actionId", ""),
        ("actionId", []),
        ("values", None),
        ("values", []),
    ],
)
def test_only_well_formed_submissions_become_events(field, value):
    assert inbound.letters(_body(**{field: value})) == []


@pytest.mark.parametrize("value", [None, "", " ", [], {}, 123])
def test_a_merchant_identifier_must_be_a_nonempty_string(value):
    body = _body()
    body["context"]["flowJSON"]["data"]["merchant_id"] = value
    assert inbound.letters(body) == []
