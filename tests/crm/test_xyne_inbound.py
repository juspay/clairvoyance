"""XyneSpaces' inbound face: the relay's signature, handshake, and envelope.

The face's own half only — verify, challenge, and the walk from the relay's
flow_action submission to a letter that still names a provider OWNER. Who
that owner belongs to is connectivity/ingress.py's business, tested in
test_connectivity_ingress.py; the record door that mounts it is tested in
test_ingress_door.py. Mirrors test_meta_inbound.py's shape.
"""

import hashlib
import hmac

import pytest

from app.crm.connectivity.providers.xyne import inbound
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION

SECRET = "xyne-secret-for-tests"
TOKEN = "xyne-verify-token-for-tests"
ACCOUNT = "flow-account-xyne-1"
EVENT_ID = "ev_0001"
TS = "1788177600"


@pytest.fixture
def secrets(monkeypatch):
    """Both platform secrets present — the normal running state."""
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_VERIFY_TOKEN", TOKEN)


def _sig(raw: bytes, secret: str = SECRET) -> str:
    """A valid x-xyne-signature over these exact bytes."""
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _body(**overrides) -> dict:
    """The relay's flow_action submission."""
    fields = {
        "flow_action": {
            "type": "flow_action",
            "id": EVENT_ID,
            "account_id": ACCOUNT,
            "performed_at": TS,
        },
        "customer_mobile_number": "919876543210",
        "customer_name": "Ada",
    }
    fields.update(overrides)
    return fields


# --- their signature ---------------------------------------------------------


def test_a_correct_signature_over_these_exact_bytes_passes(secrets) -> None:
    """A correct signature over these exact bytes passes."""
    raw = b'{"flow_action": {"type": "flow_action"}}'
    assert inbound.verify_signature(raw, {"X-Xyne-Signature": _sig(raw)})


def test_the_header_is_matched_whatever_its_casing(secrets) -> None:
    """The header is matched whatever its casing."""
    raw = b"{}"
    assert inbound.verify_signature(raw, {"x-xyne-signature": _sig(raw)})


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


def test_a_missing_header_or_bad_prefix_is_refused(secrets) -> None:
    """A missing header, or one without the prefix, is refused."""
    assert not inbound.verify_signature(b"{}", {})
    assert not inbound.verify_signature(b"{}", {"x-xyne-signature": "sha1=abc"})


def test_no_secret_configured_refuses_everything(monkeypatch) -> None:
    """Empty secret is a legal boot but refuses everything — fail closed."""
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_SECRET", "")
    raw = b"{}"
    assert not inbound.verify_signature(raw, {"x-xyne-signature": _sig(raw)})


# --- their handshake ---------------------------------------------------------


def test_a_matching_token_echoes_the_challenge(secrets) -> None:
    """A matching token echoes the challenge back."""
    assert (
        inbound.handshake_challenge(
            {"mode": "subscribe", "verify_token": TOKEN, "challenge": "c-1"}
        )
        == "c-1"
    )


def test_a_wrong_token_refuses(secrets) -> None:
    """A wrong token refuses the handshake."""
    assert (
        inbound.handshake_challenge(
            {"mode": "subscribe", "verify_token": "nope", "challenge": "c-1"}
        )
        is None
    )


def test_a_missing_mode_or_challenge_refuses(secrets) -> None:
    """Missing mode or challenge refuses."""
    assert inbound.handshake_challenge({"verify_token": TOKEN}) is None
    assert (
        inbound.handshake_challenge({"mode": "subscribe", "verify_token": TOKEN})
        is None
    )


# --- their envelope ----------------------------------------------------------


def test_a_flow_action_submission_yields_one_letter(secrets) -> None:
    """A flow_action submission yields one letter naming the flow account."""
    out = inbound.letters(_body())
    assert len(out) == 1
    letter = out[0]
    assert letter.owner_kind == inbound.OWNER_ACCOUNT
    assert letter.owner_id == ACCOUNT
    assert letter.topic == TOPIC_FLOW_ACTION
    assert letter.external_id == f"{ACCOUNT}:{EVENT_ID}"
    assert letter.payload["customer_mobile_number"] == "919876543210"


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


def test_letters_carry_the_relays_clock(secrets) -> None:
    """Letters carry the relay's clock."""
    out = inbound.letters(_body())
    assert out[0].occurred_at is not None
    assert int(out[0].occurred_at.timestamp()) == int(TS)


def test_an_unusable_timestamp_never_raises(secrets) -> None:
    """An unusable timestamp never raises."""
    out = inbound.letters(
        _body(
            flow_action={
                "type": "flow_action",
                "id": EVENT_ID,
                "account_id": ACCOUNT,
                "performed_at": "soon",
            }
        )
    )
    assert out[0].occurred_at is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"flow_action": "nope"},
        {"flow_action": {"type": "flow_action", "id": EVENT_ID}},
        {"flow_action": {"type": "flow_action", "account_id": ACCOUNT}},
        {"account_id": ACCOUNT},
        {"id": EVENT_ID},
        {"messageId": EVENT_ID},
    ],
)
def test_an_unattributable_envelope_yields_nothing_and_raises_nothing(body) -> None:
    """An unattributable envelope — no owner or no event id — yields nothing.

    GENERIC: whatever the type field (or the absence of one) says, a signed
    event we cannot attribute to an account and an event id yields no letters
    and raises nothing — a guessed owner or topic is never put on the spine.
    """
    assert inbound.letters(body) == []


def test_an_event_files_even_if_its_type_is_not_the_flows_own_word(secrets) -> None:
    """No `type` discriminator is consulted: an attributable event files.

    The body names an account and an event id; the sender's `type` is their
    transport's vocabulary, not something we reject on. That gate is gone —
    this is exactly the standalone `postMessage` response that used to 400 on
    a missing `eventType`.
    """
    out = inbound.letters(
        _body(
            flow_action={
                "type": "flow_created",
                "id": EVENT_ID,
                "account_id": ACCOUNT,
            }
        )
    )
    assert len(out) == 1
    assert out[0].owner_id == ACCOUNT
    assert out[0].external_id == f"{ACCOUNT}:{EVENT_ID}"
