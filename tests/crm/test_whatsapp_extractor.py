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
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.crm.record.workers as workers
from app.crm.outreach.nodes.context import reply_key
from app.crm.outreach.schemas import WorkflowNode
from app.crm.outreach.walker import pick_next
from app.crm.record import catalog
from app.crm.record.contracts import (
    canonical_path,
    derive_for,
    field_value,
)
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


# --- the package's public surface is the registry, nothing else ---------------


def test_the_package_exports_the_spec_contract_and_nothing_else() -> None:
    """SOURCE · ENTRIES · DERIVERS are what record/catalog.py reads off a
    SPEC_MODULES entry — and ALL the package exports. Every other name is
    imported by full path (whatsapp.flow.flow_response): an __init__ that
    re-exports its siblings is the 132-line accessor hub scar (modules/00
    §1), and this pin is what stops it growing back into one."""
    assert whatsapp_spec.__all__ == ["SOURCE", "ENTRIES", "DERIVERS"]


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


def _submission(response: str, **overrides) -> Dict[str, Any]:
    """A completed Flow, as Meta files it: an ordinary inbound message of
    type 'interactive' whose interactive.type is 'nfm_reply'."""
    return _inbound(
        _message(
            type="interactive",
            interactive={
                "type": "nfm_reply",
                "nfm_reply": {
                    "name": "flow",
                    "body": "Sent",
                    **overrides,
                    "response_json": response,
                },
            },
        )
    )


def test_a_submitted_form_answers_the_square_that_was_waiting() -> None:
    """A Flow submission returns the branchable token FORM_SUBMITTED so
    the walker picks the labelled arrow.  Before this, reply was the raw
    JSON blob — it matched no label, the square took the else arrow or
    exited 'completed', and a customer who HAD answered was lost.

    The VALUE is pinned literally, not through the constant: published
    plans store this word in their edges, so changing the constant would
    orphan every arrow already labelled with it."""
    extracted = _extract_inbound(
        _submission('{"flow_token":"m-42","address":"221B Baker Street"}')
    )
    assert extracted.variables["reply"] == "form_submitted"
    assert whatsapp_spec.flow.FORM_SUBMITTED == "form_submitted"


def test_the_form_reads_as_lines_for_whoever_opens_the_order() -> None:
    """It lands in a merchant's order note, so it is written out one
    answer per line in the order her form asked — not as the JSON Meta
    encoded it in. Her keys and values are never renamed or reworded."""
    assert whatsapp_spec.flow.flow_response(
        _submission('{"address":"221B Baker Street","city":"Bengaluru"}')
    ) == ("address: 221B Baker Street\ncity: Bengaluru")


def test_a_multi_select_reads_as_a_list_a_person_would_write() -> None:
    """Meta sends several answers to one question as a list; `['a', 'b']`
    in an order note is a python repr leaking into a merchant's shop."""
    assert (
        whatsapp_spec.flow.flow_response(_submission('{"slots":["morning","evening"]}'))
        == "slots: morning, evening"
    )


def test_our_own_token_comes_out_of_what_a_plan_reads() -> None:
    """The send stamps flow_token and Meta echoes it back inside her
    answers. Left in, a merchant's order note reads a uuid of ours beside
    her address. Her keys are untouched; only ours comes out.

    Declared `variable`, so it rides out of the engine into the woken
    square's facts and answers {facts_<square>_flow_response} in an action
    square's args — without the flag the answers are decoded, stored and
    unreachable, because only a declared variable field produces a name the
    publish validator will accept."""
    extracted = _extract_inbound(
        _submission('{"address":"221B Baker Street","flow_token":"m-42"}')
    )
    assert extracted.variables["flow_response"] == "address: 221B Baker Street"
    # The join key is still read — from the raw submission, not from the
    # string above, which no longer carries it.
    assert (
        engine.field_value(
            _submission('{"address":"221B","flow_token":"m-42"}'),
            "flow_token",
            catalog.derive_for("whatsapp", "message.inbound"),
        )
        == "m-42"
    )


def test_an_unparseable_submission_is_still_her_best_answer() -> None:
    """Nothing to remove and nothing we can re-encode: hand back exactly
    what arrived rather than dropping a customer's answer on a shape we
    did not expect."""
    assert whatsapp_spec.flow.flow_response(_submission("not json")) == "not json"


def test_a_submission_of_nothing_but_our_token_is_absent() -> None:
    """A form that answered nothing leaves nothing — the square parks on
    the missing fact rather than writing "{}" into a merchant's order."""
    assert (
        whatsapp_spec.flow.flow_response(_submission('{"flow_token":"m-42"}')) is None
    )


def test_a_form_that_collects_NOTHING_still_wakes_the_square() -> None:
    """The other half of the test above, and the one that matters more.

    ``flow_response`` is empty for a form that collected no fields — and
    that is not the same fact as "no form arrived". Two Flows Meta
    documents submit exactly this: a confirm-only one (she taps accept and
    there is nothing to collect) and an endpoint-backed one, whose answers
    went to the merchant's own server during the conversation so the
    closing payload is empty. response_json still carries OUR flow_token,
    which flow_response drops.

    Asking flow_response whether a form arrived therefore read both as
    silence: reply was None, the listening square never woke, and the run
    waited out its timeout chasing an answer she had already given. That is
    precisely the bug `reply` exists to prevent, so the discriminant is the
    SUBMISSION, never its contents.
    """
    only_our_token = _submission('{"flow_token":"m-42"}')

    extracted = _extract_inbound(only_our_token)
    assert extracted.variables["reply"] == "form_submitted"
    # She submitted, and there is genuinely nothing of hers to render — so
    # the answer is absent rather than empty, and a plan that maps it parks
    # loudly instead of sending a blank line to a customer.
    assert "flow_response" not in extracted.variables
    # The join still resolves. flow_token is KEYABLE, not a variable — a
    # listening square matches on it, no template ever prints it — so it is
    # read the way `match` reads it rather than looked for among the blanks.
    assert (
        field_value(
            only_our_token,
            canonical_path("flow_token"),
            derive_for(whatsapp_spec.SOURCE, "message.inbound"),
        )
        == "m-42"
    )


def test_a_submission_is_recognised_by_its_envelope_not_by_its_payload() -> None:
    """Every shape Meta can put in response_json is still a completed
    form — an empty object, and text that does not parse at all. None of
    them may read as silence; only a message that is NOT an nfm_reply may.
    """
    for response in ('{"flow_token":"m-42"}', "{}", "not json at all"):
        assert (
            whatsapp_spec.flow.raw_submission(_submission(response)) is not None
        ), response
        assert (
            whatsapp_spec.inbound.reply(_submission(response)) == "form_submitted"
        ), response

    # And the guard holds the other way: a tap that happens to carry an
    # nfm_reply member answers with its own id, not with the form's word.
    tap = _inbound(
        _message(
            type="interactive",
            interactive={
                "type": "button_reply",
                "button_reply": {"id": "CONFIRM"},
                "nfm_reply": {"response_json": '{"flow_token":"m-42"}'},
            },
        )
    )
    assert whatsapp_spec.inbound.reply(tap) == "CONFIRM"
    assert whatsapp_spec.flow.raw_submission(tap) is None


def test_a_submission_names_the_send_that_opened_it() -> None:
    """The send stamps the manifest row's own id as the flow_token
    (whatsapp/adapter.py) and Meta returns it verbatim — so unlike
    replied_to, this join needs no wamid on either side."""
    extracted = _extract_inbound(
        _submission('{"flow_token":"m-42","address":"221B Baker Street"}')
    )
    assert (
        engine.field_value(
            _submission('{"flow_token":"m-42"}'),
            "flow_token",
            catalog.derive_for("whatsapp", "message.inbound"),
        )
        == "m-42"
    )
    assert extracted.about == "customer"


def test_metas_placeholder_token_is_not_an_id() -> None:
    """A send that named no token gets Meta's literal 'unused' back. It
    identifies nothing, so it is dropped rather than stored as a join key
    that would match every other tokenless send."""
    assert whatsapp_spec.flow.flow_token(_submission('{"flow_token":"unused"}')) is None


def test_a_malformed_submission_is_absent_not_a_raise() -> None:
    """The decode step reads a whole batch; one unparseable letter must not
    strand the rows beside it."""
    assert whatsapp_spec.flow.flow_token(_submission("not json at all")) is None
    assert whatsapp_spec.flow.flow_token(_submission("[1, 2]")) is None
    assert whatsapp_spec.flow.flow_response(_inbound()) is None
    assert whatsapp_spec.flow.flow_token(_inbound()) is None


def test_a_button_tap_still_answers_with_its_payload() -> None:
    """The Flow fallback sits BELOW the button branches: a tap must not
    start answering with a form's JSON."""
    tap = _inbound(_message(type="button", button={"payload": "CONFIRM", "text": "C"}))
    assert whatsapp_spec.inbound.reply(tap) == "CONFIRM"
    assert whatsapp_spec.flow.flow_response(tap) is None


def test_a_choice_carrying_a_stray_submission_is_not_read_as_one() -> None:
    """BOTH discriminants decide, not the member's presence: the message
    must be type 'interactive' AND interactive.type must be 'nfm_reply'.

    Reading the member alone would let a button_reply that happens to carry
    one populate flow_token — which is KEYABLE, so a listening square could
    then be woken by a letter naming another run's message id."""
    stray = _inbound(
        _message(
            type="interactive",
            text=None,
            interactive={
                "type": "button_reply",
                "button_reply": {"id": "CANCEL_ORDER", "title": "Cancel"},
                "nfm_reply": {"response_json": '{"flow_token":"m-99"}'},
            },
        )
    )
    assert whatsapp_spec.flow.flow_response(stray) is None
    assert whatsapp_spec.flow.flow_token(stray) is None
    assert whatsapp_spec.inbound.reply(stray) == "CANCEL_ORDER"


def test_a_submission_picks_the_arrow_an_author_labelled() -> None:
    """The whole point of the token: the walker branches by comparing the
    answer to arrow labels, so it must BE a label.

    Driven through the walker's own pick_next rather than asserted at the
    extractor, because the two are one contract — a blob here equalled no
    label, and the square either took the else arrow or ended the run at
    the instant she succeeded."""
    answer = _extract_inbound(
        _submission('{"flow_token":"m-42","address":"221B Baker Street"}')
    ).variables["reply"]

    square = WorkflowNode(
        id="wait-reply",
        type="wait_event",
        topics=["message.inbound"],
        key="reply",
        minutes=2880,
    )
    arrows: List[Tuple[str, Optional[str]]] = [
        ("tag-confirmed", "CONFIRM"),
        ("save-address", "form_submitted"),
        ("tag-no-response", "timeout"),
    ]
    assert pick_next(square, arrows, {reply_key(square.id): answer}) == "save-address"

    # And the arrow it must NOT take: a board with no matching label ends
    # the run, which is the failure this token exists to prevent.
    unlabelled: List[Tuple[str, Optional[str]]] = [
        ("tag-confirmed", "CONFIRM"),
        ("tag-no-response", "timeout"),
    ]
    assert pick_next(square, unlabelled, {reply_key(square.id): answer}) is None


def test_the_recorded_fixtures_pin_both_reply_shapes() -> None:
    """The recorded fixtures pin both reply shapes."""
    # The same pin Meta's docs cannot drift past: the recorded button
    # letter answers with its payload, the recorded text letter with its
    # body (tests/crm/fixtures/whatsapp/).
    folder = Path(__file__).parent / "fixtures" / "whatsapp"
    button = json.loads((folder / "message_inbound_reply.json").read_text())
    text = json.loads((folder / "message_inbound.json").read_text())
    assert whatsapp_spec.inbound.reply(button) == "CONFIRM_ORDER"
    assert whatsapp_spec.inbound.reply(text) == "yes, confirm my order"


# --- statuses: what became of a message we sent -------------------------------


def test_a_receipt_is_about_the_message_not_a_person() -> None:
    # Canon T13 col 14 lists receipts beside template letters: processed
    # but not about a person, customer NULL forever. The join to WHO we
    # messaged lives on the manifest row (crm_message is born with
    # customer_id); resolving the recipient here would run resolve()
    # three times per send — sent, delivered, read — to duplicate it.
    """A receipt is about the message, not a person."""
    assert STATUS_SPEC is not None
    assert STATUS_SPEC.about == "merchant" and STATUS_SPEC.identity == {}
    extracted = _extract_status(_status())
    assert extracted.about == "merchant"
    assert extracted.handles == {} and extracted.facts == {}


# --- merchant letters: the WABA's news, no person in them ---------------------


@pytest.mark.parametrize(
    "topic",
    ["template.status", "template.category", "template.quality", "account.update"],
)
def test_letters_about_the_waba_are_declared_merchant_level(topic) -> None:
    # Template and account letters ride the same source but concern the
    # WABA, not a customer. Declared about="merchant" with no identity
    # fields, they decode with no handles and the worker stamps them
    # processed with customer NULL — no resolve, no quarantine (canon T13
    # col 14). Undeclared, they fell to the flat shape, quarantined
    # no_handle, and the template-status consumer never heard a letter.
    """Letters about the WABA are declared merchant-level."""
    spec = catalog.code_spec("whatsapp", topic)
    assert spec is not None and spec.about == "merchant"
    assert spec.identity == {}


def test_a_template_review_decodes_with_its_facts_and_no_handles() -> None:
    """A template review decodes with its facts and no handles."""
    payload = {
        "event": "REJECTED",
        "message_template_id": 1953621873,
        "message_template_name": "diwali_blowout_sale",
        "message_template_language": "en_US",
        "reason": "INCORRECT_CATEGORY",
    }
    spec = catalog.code_spec("whatsapp", "template.status")
    assert spec is not None
    extracted = engine.extract(payload, spec)
    assert extracted.about == "merchant"
    assert extracted.handles == {} and extracted.facts == {}
    assert extracted.variables["event"] == "REJECTED"
    assert extracted.variables["reason"] == "INCORRECT_CATEGORY"
    assert extracted.variables["message_template_name"] == "diwali_blowout_sale"


def test_a_ban_notice_reads_the_state_meta_ships_in_a_list() -> None:
    # ban_info.waba_ban_state is a one-element LIST in Meta's value — the
    # deriver unwraps it; a letter that is no ban derives nothing.
    """A ban notice reads the state Meta ships in a list."""
    folder = Path(__file__).parent / "fixtures" / "whatsapp"
    ban = json.loads((folder / "account_update.json").read_text())
    assert whatsapp_spec.account.ban_state(ban) == "SCHEDULE_FOR_DISABLE"
    assert whatsapp_spec.account.ban_state({"event": "VERIFIED_ACCOUNT"}) is None


# --- refusals: skipped, not written -------------------------------------------


def test_an_unusable_number_is_skipped_not_written() -> None:
    # normalize_phone returns None rather than writing a malformed handle;
    # a bad handle would poison every later probe on it.
    """An unusable number is skipped, not written."""
    bad_inbound = _inbound(message=_message(**{"from": "n/a"}))
    assert _extract_inbound(bad_inbound).handles == {}


# --- the catalog placement and the pass ---------------------------------------


def test_whatsapp_is_a_code_catalog_source_not_an_imperative_extractor() -> None:
    # The ruled path (event-catalog.md §One decode engine): a connector
    # source is a SPEC. Listed in EXTRACTORS too, two readers of one
    # payload would drift — the disease the engine exists to end.
    """Whatsapp is a code-catalog source, not an imperative extractor."""
    topics = (
        "message.inbound",
        "message.status",
        "template.status",
        "template.category",
        "template.quality",
        "account.update",
    )
    for topic in topics:
        assert ("whatsapp", topic) in catalog.CATALOG
        assert set(catalog.derive_for("whatsapp", topic)) <= set(whatsapp_spec.DERIVERS)
    assert "whatsapp" not in EXTRACTORS


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


async def test_the_pass_stamps_a_merchant_letter_with_no_customer(
    monkeypatch,
) -> None:
    # The merchant end of the chain: a template review reaches every
    # consumer with customer_id None and stamps processed — no resolve, no
    # quarantine. Before the entries existed, this exact letter quarantined
    # no_handle and the template-status consumer heard nothing.
    """The pass stamps a merchant letter with no customer."""
    seen: Dict[str, Any] = {}

    async def never_resolve(*args: Any, **kwargs: Any) -> str:
        """Test double: a merchant letter must never resolve()."""
        raise AssertionError("a merchant letter must never resolve()")

    async def never_quarantine(*args: Any, **kwargs: Any) -> None:
        """Test double: a merchant letter must never quarantine."""
        raise AssertionError("a merchant letter must never quarantine")

    async def fake_consume(event, customer_id, handles, variables) -> None:
        """Test double: record what the consumer slot receives."""
        seen["customer_id"] = customer_id
        seen["variables"] = variables

    async def fake_stamp(txn, event_id, customer_id) -> None:
        """Test double: record the stamp."""
        seen["stamped"] = (event_id, customer_id)

    monkeypatch.setattr(workers, "crm_resolve", never_resolve)
    monkeypatch.setattr(workers, "consumers", lambda: [fake_consume])
    monkeypatch.setattr(workers.accessor, "stamp_event", fake_stamp)
    monkeypatch.setattr(workers.accessor, "quarantine_event", never_quarantine)

    folder = Path(__file__).parent / "fixtures" / "whatsapp"
    event = RawEvent(
        id="e2",
        merchant_id="m1",
        source="whatsapp",
        topic="template.status",
        schema_version="v23.0",
        external_id="waba:1953621873:REJECTED:1",
        payload=json.loads((folder / "template_status_rejected.json").read_text()),
        received_at=datetime.now(timezone.utc),
    )
    await workers._process_one(None, event)  # type: ignore[arg-type]

    assert seen["customer_id"] is None
    assert seen["stamped"] == ("e2", None)
    assert seen["variables"]["reason"] == "INCORRECT_CATEGORY"
