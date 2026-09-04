"""WhatsApp through the one decode engine: the code-layer spec attributes
Meta's letters to a person.

The chain this pins: the ingress door files source="whatsapp" letters with
customer_id NULL, and the event worker reads each by its catalog spec.
Undeclared, whatsapp fell to the flat shape — which reads a top-level
customer_mobile_number Meta never sends — so every letter quarantined
no_handle: no resolve, no journey, no workflow entry.

The generic four-part square (fixtures exist, every field resolves, the
engine finds the person in every recorded letter, derivers match) is pinned
for ALL code entries by test_catalog.py; what lives here is whatsapp's own
behaviour — the wa_id match, the refusals, and the worker handing the
normalized phone onward.

Payloads are shaped exactly as the door files them
(providers/meta/inbound.py::_narrowed): Meta's value with the batched array
narrowed to one item, metadata and contacts riding along verbatim.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

import app.crm.record.workers as workers
from app.crm.record import catalog
from app.crm.record.extractors import EXTRACTORS, engine, whatsapp as whatsapp_spec
from app.crm.record.schemas import Extracted, RawEvent

WA_FROM = "919876543210"
OUR_NUMBER_ID = "812345678901234"

INBOUND_SPEC = catalog.code_spec("whatsapp", "message.inbound")
STATUS_SPEC = catalog.code_spec("whatsapp", "message.status")


def _message(**overrides) -> Dict[str, Any]:
    """One inbound message item, as Meta shapes it."""
    fields: Dict[str, Any] = {
        "from": WA_FROM,
        "id": "wamid.INBOUND",
        "timestamp": "1788177600",
        "type": "text",
        "text": {"body": "yes, confirm it"},
    }
    fields.update(overrides)
    return fields


def _inbound(message: Dict[str, Any] | None = None, **overrides) -> Dict[str, Any]:
    """An inbound letter's payload: the value narrowed to one message."""
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": OUR_NUMBER_ID},
        "contacts": [{"wa_id": WA_FROM, "profile": {"name": "Priya Sharma"}}],
        "messages": [_message() if message is None else message],
    }
    payload.update(overrides)
    return payload


def _status(**overrides) -> Dict[str, Any]:
    """A status letter's payload: the value narrowed to one status."""
    item: Dict[str, Any] = {
        "id": "wamid.OUTBOUND",
        "status": "delivered",
        "timestamp": "1788177600",
        "recipient_id": WA_FROM,
        "pricing": {"billable": True, "category": "utility"},
    }
    item.update(overrides)
    return {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": OUR_NUMBER_ID},
        "statuses": [item],
    }


def _extract_inbound(payload: Dict[str, Any]) -> Extracted:
    """The engine over whatsapp's inbound spec — the worker's exact read."""
    assert INBOUND_SPEC is not None
    return engine.extract(payload, INBOUND_SPEC)


def _extract_status(payload: Dict[str, Any]) -> Extracted:
    """The engine over whatsapp's status spec — the worker's exact read."""
    assert STATUS_SPEC is not None
    return engine.extract(payload, STATUS_SPEC)


# --- inbound: the customer wrote to us ----------------------------------------


def test_an_inbound_message_yields_the_senders_phone_in_e164() -> None:
    # Meta's `from` is the wa_id: country code, no "+". The stored form is
    # the probed form, so the handle must leave here already normalized.
    """An inbound message yields the sender's phone in E.164."""
    extracted = _extract_inbound(_inbound())
    assert extracted.handles["phone"] == "+919876543210"


def test_the_senders_name_is_matched_from_contacts_by_wa_id() -> None:
    # contacts[] rides parallel to messages[]; a positional read would pin
    # one person's name on another when a batch carries several senders.
    """The sender's name is matched from contacts by wa_id."""
    extracted = _extract_inbound(
        _inbound(
            contacts=[
                {"wa_id": "918888888888", "profile": {"name": "Somebody Else"}},
                {"wa_id": WA_FROM, "profile": {"name": "Priya Sharma"}},
            ]
        )
    )
    assert extracted.facts == {"name": "Priya Sharma"}


@pytest.mark.parametrize("contacts", [None, [], [{"wa_id": "918888888888"}]])
def test_a_letter_without_the_senders_contact_still_attributes(contacts) -> None:
    # Meta may omit contacts, and a contact for someone else lends no name.
    # Attribution must not hinge on the name: absent is absent, the phone
    # is what resolves.
    """A letter without the sender's contact still attributes."""
    extracted = _extract_inbound(_inbound(contacts=contacts))
    assert extracted.handles["phone"] == "+919876543210"
    assert "name" not in extracted.facts


def test_a_blank_profile_name_is_not_a_fact() -> None:
    # Never defaulted, never padded: a blank reaching assert_facts would be
    # a genuine claim overwriting what we actually know about the person.
    """A blank profile name is not a fact."""
    extracted = _extract_inbound(
        _inbound(contacts=[{"wa_id": WA_FROM, "profile": {"name": "   "}}])
    )
    assert "name" not in extracted.facts


def test_a_shared_contact_card_is_not_read_as_the_sender() -> None:
    # On a type="contacts" message the CARDS the customer shared live inside
    # the message item; the sender roster stays at the value level. Only the
    # roster is read: the plumber's card must never become anyone's name.
    """A shared contact card is not read as the sender."""
    cards = [{"name": {"formatted_name": "My Plumber"}, "phones": [{"phone": "+1555"}]}]
    extracted = _extract_inbound(
        _inbound(message=_message(type="contacts", contacts=cards), contacts=[])
    )
    assert extracted.handles["phone"] == "+919876543210"
    assert "name" not in extracted.facts


def test_the_declared_variables_ride_out_for_templates() -> None:
    # What the catalog marks variable=True is what a template may fill.
    """The declared variables ride out for templates."""
    extracted = _extract_inbound(_inbound())
    assert extracted.variables["message_text"] == "yes, confirm it"
    assert extracted.variables["sender_name"] == "Priya Sharma"


# --- reply: what the customer answered, whichever widget carried it -----------


def test_a_button_tap_answers_with_the_registered_payload() -> None:
    """A button tap answers with the registered payload."""
    # A template quick-reply carries no text.body; the answer is the
    # button's payload — stable across languages, unlike its label.
    extracted = _extract_inbound(
        _inbound(
            _message(
                type="button",
                text=None,
                button={"payload": "CONFIRM_ORDER", "text": "Confirm order"},
            )
        )
    )
    assert extracted.variables["reply"] == "CONFIRM_ORDER"
    assert "message_text" not in extracted.variables


def test_an_interactive_choice_answers_with_its_id() -> None:
    """An interactive choice answers with its id."""
    extracted = _extract_inbound(
        _inbound(
            _message(
                type="interactive",
                text=None,
                interactive={
                    "type": "button_reply",
                    "button_reply": {"id": "CANCEL_ORDER", "title": "Cancel"},
                },
            )
        )
    )
    assert extracted.variables["reply"] == "CANCEL_ORDER"


def test_a_typed_answer_falls_back_to_the_text_body() -> None:
    """A typed answer falls back to the text body."""
    # She ignored the buttons and wrote instead: still an answer, on the
    # same key a wait_event square branches on.
    extracted = _extract_inbound(_inbound())
    assert extracted.variables["reply"] == "yes, confirm it"


def test_the_recorded_fixtures_pin_both_reply_shapes() -> None:
    """The recorded fixtures pin both reply shapes."""
    # The same pin Meta's docs cannot drift past: the recorded button
    # letter answers with its payload, the recorded text letter with its
    # body (tests/crm/fixtures/whatsapp/).
    folder = Path(__file__).parent / "fixtures" / "whatsapp"
    button = json.loads((folder / "message_inbound_reply.json").read_text())
    text = json.loads((folder / "message_inbound.json").read_text())
    assert whatsapp_spec.reply(button) == "CONFIRM_ORDER"
    assert whatsapp_spec.reply(text) == "yes, confirm my order"


# --- statuses: what became of a message we sent -------------------------------


def test_a_status_yields_the_recipients_phone_and_no_facts() -> None:
    # The receipt attributes to the customer it is about — in practice a
    # lookup, since we messaged them — but it says nothing ABOUT them.
    """A status yields the recipient's phone and no facts."""
    extracted = _extract_status(_status())
    assert extracted.handles == {"phone": "+919876543210"}
    assert extracted.facts == {}


def test_a_status_missing_its_recipient_yields_nothing() -> None:
    """A status missing its recipient yields nothing."""
    extracted = _extract_status(_status(recipient_id=None))
    assert extracted.handles == {}


# --- refusals: skipped, not written -------------------------------------------


@pytest.mark.parametrize("topic", ["template.status", "account.update"])
def test_letters_naming_no_person_have_no_code_spec(topic) -> None:
    # Template and account letters ride the same source but concern the
    # WABA, not a customer — undeclared, they decode by the flat shape,
    # yield no handle, and quarantine no_handle (replayable) until a
    # consumer learns to read them.
    """Letters naming no person have no code spec."""
    assert catalog.code_spec("whatsapp", topic) is None
    payload = {"event": "APPROVED", "message_template_id": "t-1"}
    extracted = engine.extract(payload, engine.EMPTY_SPEC)
    assert extracted.handles == {} and extracted.facts == {}


def test_an_unusable_number_is_skipped_not_written() -> None:
    # normalize_phone returns None rather than writing a malformed handle;
    # a bad handle would poison every later probe on it.
    """An unusable number is skipped, not written."""
    bad_inbound = _inbound(message=_message(**{"from": "n/a"}))
    assert _extract_inbound(bad_inbound).handles == {}
    assert _extract_status(_status(recipient_id="n/a")).handles == {}


# --- the catalog placement and the pass ---------------------------------------


def test_whatsapp_is_a_code_catalog_source_not_an_imperative_extractor() -> None:
    # The ruled path (event-catalog.md §One decode engine): a connector
    # source is a SPEC. Listed in EXTRACTORS too, two readers of one
    # payload would drift — the disease the engine exists to end.
    """Whatsapp is a code-catalog source, not an imperative extractor."""
    assert ("whatsapp", "message.inbound") in catalog.CATALOG
    assert ("whatsapp", "message.status") in catalog.CATALOG
    assert "whatsapp" not in EXTRACTORS
    assert set(catalog.derive_for("whatsapp", "message.inbound")) <= set(
        whatsapp_spec.DERIVERS
    )


async def test_the_pass_hands_the_normalized_phone_to_the_consumers(
    monkeypatch,
) -> None:
    # End of the chain: the number the consumers see is the number identity
    # resolved on, so suppression matches by construction — and the
    # catalog's variables ride beside it.
    """The pass hands the normalized phone to the consumers."""
    seen: Dict[str, Any] = {}

    async def fake_resolve(merchant_id, handles, evidence, source):
        """Test double: resolve without a database."""
        seen["resolved_on"] = handles
        return "cus-1"

    async def fake_facts(*args: Any, **kwargs: Any) -> None:
        """Test double: swallow the name claim."""
        return None

    async def fake_consume(event, customer_id, handles, variables) -> None:
        """Test double: record what the consumer slot receives."""
        seen["handles"] = handles
        seen["variables"] = variables

    async def fake_stamp(*args: Any, **kwargs: Any) -> None:
        """Test double: swallow the stamp."""
        return None

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)
    monkeypatch.setattr(workers, "assert_facts", fake_facts)
    monkeypatch.setattr(workers, "consumers", lambda: [fake_consume])
    monkeypatch.setattr(workers.accessor, "stamp_event", fake_stamp)

    event = RawEvent(
        id="e1",
        merchant_id="m1",
        source="whatsapp",
        topic="message.inbound",
        schema_version="1",
        external_id="wamid.INBOUND",
        payload=_inbound(),
        received_at=datetime.now(timezone.utc),
    )
    await workers._process_one(None, event)  # type: ignore[arg-type]

    assert seen["resolved_on"]["phone"] == "+919876543210"
    assert seen["handles"] == seen["resolved_on"]
    assert seen["variables"]["message_text"] == "yes, confirm it"
