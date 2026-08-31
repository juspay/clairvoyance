"""queue_message: the first producer's obligations — the vocabulary refuses
what it does not know, the writer normalizes the address, and the dedupe
unique makes a retry a no-op (None), never a second row."""

import asyncio
from typing import Any, List, Optional

import pytest

import app.crm.connectivity.queue as queue
from app.crm.connectivity.db.queries import insert_message_query


def _proposal(**overrides: Any) -> dict:
    base = dict(
        merchant_id="m1",
        customer_id="c1",
        channel="whatsapp",
        address="98450 12345",
        source_kind="workflow",
        source_id="run-1",
        purpose_key="utility.order.cod_confirm",
        template_id="cod_confirm",
        variables={"name": "Priya"},
        dedupe_key="run-1:ask",
    )
    base.update(overrides)
    return base


def test_unknown_source_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        queue.validate_proposal("cron", "utility.order")


def test_purpose_must_start_with_a_known_root() -> None:
    with pytest.raises(ValueError):
        queue.validate_proposal("workflow", "cod_confirm")
    queue.validate_proposal("workflow", "utility.order.cod_confirm")


def test_writer_normalizes_the_address() -> None:
    assert queue.normalize_address("whatsapp", "98450 12345") == "+919845012345"
    assert queue.normalize_address("email", " Priya@Shop.IN ") == "priya@shop.in"
    assert queue.normalize_address("whatsapp", "hello") is None


def test_insert_is_a_queued_row_absorbed_by_the_dedupe_unique() -> None:
    sql, values = insert_message_query(
        "m1",
        "c1",
        "whatsapp",
        "+919845012345",
        "workflow",
        "run-1",
        "utility.order",
        "cod_confirm",
        {"name": "Priya"},
        "run-1:ask",
    )
    assert "ON CONFLICT (merchant_id, dedupe_key) DO NOTHING" in sql
    assert "status" not in sql  # the column default: queued, no verdict
    assert values[-1] == "run-1:ask" and values[8] == '{"name": "Priya"}'


def test_queue_message_returns_id_then_none_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[str] = []

    async def fake_insert(*args: Any) -> Optional[str]:
        seen.append(args[3])  # the normalized address
        return "msg-1" if len(seen) == 1 else None

    monkeypatch.setattr(queue.accessor, "insert_message", fake_insert)
    first = asyncio.run(queue.queue_message(**_proposal()))
    second = asyncio.run(queue.queue_message(**_proposal()))
    assert (first, second) == ("msg-1", None)
    assert seen == ["+919845012345", "+919845012345"]


def test_unusable_address_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args: Any) -> Optional[str]:
        raise AssertionError("must not write")

    monkeypatch.setattr(queue.accessor, "insert_message", never)
    with pytest.raises(ValueError):
        asyncio.run(queue.queue_message(**_proposal(address="hello")))
