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
from typing import Any, List, Optional

import pytest

from app.crm.connectivity import receipts
from app.crm.connectivity.db.queries.message import apply_receipt_query
from app.crm.connectivity.schemas.message import ProviderReceipt
from app.crm.connectivity.status import (
    MESSAGE_ACCEPTED,
    MESSAGE_DEAD,
    MESSAGE_DELIVERED,
    MESSAGE_FAILED,
    MESSAGE_READ,
    MESSAGE_SENT,
)
from app.crm.connectivity.topics import TOPIC_STATUS
from app.crm.record import catalog
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


async def test_a_receipt_for_a_message_not_ours_is_quiet(monkeypatch) -> None:
    """Sent from the provider's own app on the same number: no row, no raise
    (a raise would retry the letter and finally quarantine it for nothing)."""

    async def apply(*args: Any) -> None:
        return None

    monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
    await receipts.consume_status_event(_event("message_status.json"), None)


class _Parking:
    """The receipts module's two stores: the manifest (apply answers in
    order from ``applies``) and the parking table."""

    def __init__(self, applies: List[Optional[str]]) -> None:
        self.applies = list(applies)
        self.parked: List[Any] = []
        self.unparked: List[tuple] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def apply(*args: Any) -> Optional[str]:
            return self.applies.pop(0) if self.applies else None

        async def park(merchant_id: str, receipt: Any) -> None:
            self.parked.append(receipt)

        async def unpark(merchant_id: str, provider_id: str, state: str) -> None:
            self.unparked.append((provider_id, state))

        monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
        monkeypatch.setattr(receipts.receipt_accessor, "park_receipt", park)
        monkeypatch.setattr(receipts.receipt_accessor, "unpark", unpark)


async def test_a_receipt_that_beats_its_rows_stamp_is_parked_not_retried(
    monkeypatch,
) -> None:
    """A fast webhook consumed before the dispatcher stamped the provider's
    id: parked for the stamp's own drain. No raise — the event worker would
    retry it at once, five times, and quarantine it."""
    world = _Parking([None, None])
    world.install(monkeypatch)
    young = _event("message_status.json", received_at=datetime.now(timezone.utc))
    await receipts.consume_status_event(young, None)
    assert [r.state for r in world.parked] == [MESSAGE_DELIVERED]
    assert world.unparked == []


async def test_a_stamp_that_lands_while_parking_is_applied_at_once(monkeypatch) -> None:
    """Parked, then looked at once more: the stamp landed in between, so the
    receipt applies now and its parked copy is removed."""
    world = _Parking([None, MESSAGE_DELIVERED])
    world.install(monkeypatch)
    young = _event("message_status.json", received_at=datetime.now(timezone.utc))
    await receipts.consume_status_event(young, None)
    assert len(world.parked) == 1 and len(world.unparked) == 1


async def test_an_old_unmatched_receipt_is_not_ours_and_is_not_parked(
    monkeypatch,
) -> None:
    world = _Parking([None])
    world.install(monkeypatch)
    await receipts.consume_status_event(_event("message_status.json"), None)
    assert world.parked == []


class _Drain:
    """The drain's atom, faked: ``atomically`` runs the body with a sentinel
    txn; the parked rows and the manifest answer from lists."""

    TXN = object()

    def __init__(self, parked: List[str], matches: Optional[set] = None) -> None:
        self.parked = parked
        self.matches = set(parked) if matches is None else matches
        self.applied: List[str] = []
        self.unparked: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def atomically(fn, *args):
            return await fn(self.TXN, *args)

        async def lock(txn, merchant_id: str, provider_id: str):
            assert txn is self.TXN
            return [
                ProviderReceipt(provider_message_id=provider_id, state=state)
                for state in self.parked
            ]

        async def apply(merchant_id, provider_id, state, *rest, txn=None):
            assert txn is self.TXN  # inside the drain's atom, never beside it
            self.applied.append(state)
            return state if state in self.matches else None

        async def unpark(merchant_id, provider_id, state, txn=None):
            assert txn is self.TXN
            self.unparked.append(state)

        monkeypatch.setattr(receipts, "atomically", atomically)
        monkeypatch.setattr(receipts.receipt_accessor, "lock_parked", lock)
        monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
        monkeypatch.setattr(receipts.receipt_accessor, "unpark", unpark)


async def test_the_stamp_applies_what_was_parked_for_it_in_ladder_order(
    monkeypatch,
) -> None:
    """Parked in any order, applied up the ladder with 'failed' last — so a
    read-then-failed pair ends read, exactly as it would live — and each row
    leaves only with its receipt applied, inside the same atom."""
    drain = _Drain([MESSAGE_FAILED, MESSAGE_READ, MESSAGE_SENT])
    drain.install(monkeypatch)
    assert await receipts.apply_parked("shop", "wamid.9") is None
    assert drain.applied == [MESSAGE_SENT, MESSAGE_READ, MESSAGE_FAILED]
    assert drain.unparked == drain.applied


async def test_a_parked_receipt_that_still_matches_nothing_stays_parked(
    monkeypatch,
) -> None:
    drain = _Drain([MESSAGE_SENT, MESSAGE_DELIVERED], matches={MESSAGE_SENT})
    drain.install(monkeypatch)
    await receipts.apply_parked("shop", "wamid.9")
    assert drain.unparked == [MESSAGE_SENT]


async def test_a_drain_that_raises_loses_nothing_and_does_not_raise(
    monkeypatch,
) -> None:
    """The stamp already landed, so the send must not fail over its drain;
    the atom rolled back, so every receipt is still parked for the sweep."""
    drain = _Drain([MESSAGE_SENT, MESSAGE_READ])
    drain.install(monkeypatch)

    async def apply(merchant_id, provider_id, state, *rest, txn=None):
        if state == MESSAGE_READ:
            raise ConnectionError("pool gone")
        return state

    monkeypatch.setattr(receipts.message_accessor, "apply_receipt", apply)
    await receipts.apply_parked("shop", "wamid.9")  # no raise
    await receipts.apply_parked("shop", None)  # nothing to drain


async def test_the_sweep_retries_parked_drains_then_expires(monkeypatch) -> None:
    order: List[tuple] = []

    async def parked_messages(limit: int):
        return [("shop", "wamid.1"), ("mall", "wamid.2")]

    async def apply_parked(merchant_id, provider_id):
        order.append(("drain", merchant_id, provider_id))

    async def expire(grace_seconds: int) -> int:
        order.append(("expire", grace_seconds))
        return 3

    monkeypatch.setattr(receipts.receipt_accessor, "parked_messages", parked_messages)
    monkeypatch.setattr(receipts, "apply_parked", apply_parked)
    monkeypatch.setattr(receipts.receipt_accessor, "expire_parked", expire)
    assert await receipts.sweep_parked() == 3
    assert order == [
        ("drain", "shop", "wamid.1"),
        ("drain", "mall", "wamid.2"),
        ("expire", 600),
    ]


def test_parked_receipts_are_scoped_to_the_merchant_and_expire() -> None:
    from app.crm.connectivity.db.queries.receipt import (
        expire_parked_query,
        lock_parked_query,
        park_receipt_query,
        parked_messages_query,
    )

    query, values = park_receipt_query("shop", "wamid.1", MESSAGE_SENT, NOW, None, None)
    assert "ON CONFLICT (merchant_id, provider_message_id, state) DO NOTHING" in query
    query, values = lock_parked_query("shop", "wamid.1")
    assert "WHERE merchant_id = $1 AND provider_message_id = $2" in query
    # Locked, not deleted: a row leaves only once its receipt is applied.
    assert "FOR UPDATE SKIP LOCKED" in query and "DELETE" not in query
    query, values = parked_messages_query(100)
    # Only messages whose row carries the id now — a foreign receipt waits
    # out its grace without crowding the batch.
    assert "EXISTS" in query and "m.merchant_id = p.merchant_id" in query
    query, values = expire_parked_query(int(receipts.PARK_GRACE.total_seconds()))
    assert "parked_at < now()" in query and values == [600]
    assert "count(*)" in query  # a count, not every id shipped back


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
