"""Receipts move the manifest (inbox task 1): a message.status letter,
read through the catalog's declared fields, moves one crm_message row along
accepted -> sent -> delivered -> read — never backwards — or to failed with
the provider's code; what Meta billed it as is recorded too.

The recorded Meta callbacks under tests/crm/fixtures/whatsapp are the
inputs, so a change in Meta's shape fails here."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from app.crm.connectivity.db.queries.message import apply_receipt_query
from app.crm.connectivity.messaging.shared import receipts
from app.crm.connectivity.vocabulary.reasons import REASON_PROVIDER_REJECTED
from app.crm.connectivity.vocabulary.status import (
    MESSAGE_ACCEPTED,
    MESSAGE_DEAD,
    MESSAGE_DELIVERED,
    MESSAGE_FAILED,
    MESSAGE_READ,
    MESSAGE_SENT,
)
from app.crm.connectivity.vocabulary.topics import TOPIC_STATUS
from app.crm.record import catalog
from app.crm.record.db.queries import events_by_external_ids_query
from app.crm.record.schemas import RawEvent

FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp"
NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


def _event(fixture: str, topic: str = TOPIC_STATUS, **overrides: Any) -> RawEvent:
    fields = dict(
        id="e-1",
        merchant_id="shop",
        source="whatsapp",
        topic=topic,
        schema_version="v23.0",
        external_id="wamid.X:delivered",
        payload=json.loads((FIXTURES / fixture).read_text()),
        received_at=NOW,
        occurred_at=NOW,
    )
    fields.update(overrides)
    return RawEvent(**fields)


def test_a_delivered_receipt_reads_its_id_state_and_billing() -> None:
    receipt = receipts.read_receipt(_event("message_status.json"))
    assert receipt is not None
    assert receipt.state == MESSAGE_DELIVERED
    assert receipt.provider_message_id.startswith("wamid.")
    assert receipt.pricing_category == "utility"
    assert receipt.error_code is None and receipt.occurred_at == NOW


def test_a_failed_receipt_carries_the_providers_code() -> None:
    receipt = receipts.read_receipt(_event("message_status_failed.json"))
    assert receipt is not None
    assert receipt.state == MESSAGE_FAILED and receipt.error_code == "131047"


def test_a_failed_receipt_without_a_code_still_names_a_reason() -> None:
    """A row with no reason is a support ticket nobody can answer: a failure
    the provider sent no code for reads as provider_rejected, as the send
    paths already record it."""
    payload = json.loads((FIXTURES / "message_status_failed.json").read_text())
    payload["statuses"][0].pop("errors")
    receipt = receipts.read_receipt(
        _event("message_status_failed.json", payload=payload)
    )
    assert receipt is not None
    assert receipt.state == MESSAGE_FAILED
    assert receipt.error_code == REASON_PROVIDER_REJECTED


def test_a_state_off_the_ladder_is_not_a_receipt() -> None:
    payload = json.loads((FIXTURES / "message_status.json").read_text())
    payload["statuses"][0]["status"] = "deleted"
    assert receipts.read_receipt(_event("message_status.json", payload=payload)) is None
    payload["statuses"][0]["status"] = "delivered"
    payload["statuses"][0].pop("id")
    assert receipts.read_receipt(_event("message_status.json", payload=payload)) is None


async def test_the_consumer_moves_one_row_and_ignores_other_topics(
    monkeypatch,
) -> None:
    calls: List[tuple] = []

    async def apply(*args: Any) -> str:
        calls.append(args)
        return MESSAGE_DELIVERED

    monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
    await receipts.consume_status_event(_event("message_status.json"), None)
    await receipts.consume_status_event(
        _event("message_inbound.json", topic="message.inbound"), "c-1"
    )
    assert len(calls) == 1
    merchant_id, provider_id, state, occurred_at, error_code, category = calls[0]
    assert (merchant_id, state, category) == ("shop", MESSAGE_DELIVERED, "utility")


async def _no_row(*args: Any) -> None:
    return None


async def test_a_receipt_that_matches_no_row_is_let_go_quietly(monkeypatch) -> None:
    """Ahead of its row's stamp (the dispatcher reads it back) or sent from
    the provider's own app: no row, no raise — a raise would only retry the
    letter at once and finally quarantine it for nothing."""
    monkeypatch.setattr(receipts.message_accessor, "apply_receipt", _no_row)
    await receipts.consume_status_event(_event("message_status.json"), None)


def _filed(state: str) -> RawEvent:
    payload = json.loads((FIXTURES / "message_status.json").read_text())
    payload["statuses"][0]["status"] = state
    return _event("message_status.json", payload=payload, external_id=f"w:{state}")


async def test_the_stamp_reads_the_messages_filed_receipts_back_in_ladder_order(
    monkeypatch,
) -> None:
    """Straight after the dispatcher stamps the id: the message's status
    letters are read back by the ids its channel's face files them under,
    and applied up the ladder with 'failed' last — so a read-then-failed
    pair ends read, as it would live."""
    asked: List[tuple] = []
    applied: List[str] = []

    async def letters(merchant_id, source, external_ids):
        asked.append((merchant_id, source, external_ids))
        return [_filed(MESSAGE_FAILED), _filed(MESSAGE_READ), _filed(MESSAGE_SENT)]

    async def apply(merchant_id, provider_id, state, *rest):
        applied.append(state)
        return state

    monkeypatch.setattr(receipts, "letters_by_external_ids", letters)
    monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
    await receipts.backfill_early_receipts("shop", "whatsapp", "wamid.9")
    assert asked == [
        (
            "shop",
            "whatsapp",
            ["wamid.9:sent", "wamid.9:delivered", "wamid.9:read", "wamid.9:failed"],
        )
    ]
    assert applied == [MESSAGE_SENT, MESSAGE_READ, MESSAGE_FAILED]


async def test_a_read_back_that_fails_is_logged_never_raised(monkeypatch) -> None:
    """The send happened and its outcome is recorded: a failed read-back
    must not turn into a failed dispatch."""

    async def broken(*args: Any):
        raise ConnectionError("pool gone")

    monkeypatch.setattr(receipts, "letters_by_external_ids", broken)
    await receipts.backfill_early_receipts("shop", "whatsapp", "wamid.9")  # no raise


async def test_nothing_is_read_back_without_an_id_or_a_face_for_the_channel(
    monkeypatch,
) -> None:
    async def never(*args: Any):
        raise AssertionError("no read-back expected")

    monkeypatch.setattr(receipts, "letters_by_external_ids", never)
    await receipts.backfill_early_receipts("shop", "whatsapp", None)
    await receipts.backfill_early_receipts("shop", "carrier_pigeon", "id-1")


def test_the_read_back_rides_the_spines_dedupe_index_tenant_first() -> None:
    query, values = events_by_external_ids_query("shop", "whatsapp", ["w:sent"])
    assert "WHERE merchant_id = $1" in query
    assert "AND source = $2" in query and "external_id = ANY($3::text[])" in query
    assert values == ["shop", "whatsapp", ["w:sent"]]


def test_the_status_only_advances_and_failed_only_from_accepted_or_sent() -> None:
    query, values = apply_receipt_query(
        "shop", "wamid.1", MESSAGE_READ, NOW, None, "utility"
    )
    # merchant leads: another tenant's id must match nothing.
    assert "WHERE merchant_id = $1" in query and "provider_message_id = $2" in query
    assert "status IN ($7::text, $8::text)" in query  # failed from accepted/sent
    # failed and dead are terminal: a stale sent/delivered/read never moves
    # a failed row back up and hides the failure.
    assert "status NOT IN ($11::text, $12::text)" in query
    assert values[6:] == [
        MESSAGE_ACCEPTED,
        MESSAGE_SENT,
        MESSAGE_DELIVERED,
        MESSAGE_READ,
        MESSAGE_FAILED,
        MESSAGE_DEAD,
    ]
    # Timestamps are facts recorded first-seen, whatever order they arrive.
    assert "COALESCE(delivered_at, $4::timestamptz, now())" in query
    assert "COALESCE(read_at, $4::timestamptz, now())" in query
    placeholders = set(re.findall(r"\$(\d+)", query))
    assert placeholders == {str(n) for n in range(1, len(values) + 1)}


def test_the_fields_receipts_reads_are_declared_by_the_catalog() -> None:
    """receipts.py spells the four names; record's WhatsApp spec declares
    them (rule 12 forbids the import). Drift would silently stop every row
    at 'accepted' again."""
    derived = catalog.derive_for("whatsapp", TOPIC_STATUS)
    for name in (
        receipts.FIELD_STATE,
        receipts.FIELD_MESSAGE_ID,
        receipts.FIELD_ERROR,
        receipts.FIELD_CATEGORY,
    ):
        assert name in derived, f"message.status does not declare {name}"


def test_the_receipts_consumer_is_registered() -> None:
    import app.crm.worker_main  # noqa: F401 — registration runs at import
    from app.crm.record.consumers import consumers

    assert receipts.consume_status_event in consumers()
