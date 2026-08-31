"""The walker role: its registration, the hourly sweep riding its claim; and
the entry-rules consumer: one row in,
goal before entry, unmatched topics ignored, and a failure that propagates
(the event worker's savepoint is what isolates it — never a swallow here)."""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, List

import pytest

import app.crm.outreach.entry as entry
import app.crm.outreach.workers as outreach_workers
from app.crm.outreach.nodes import send_variables
from app.crm.outreach.schemas import Workflow, WorkflowDefinition
from app.crm.outreach.walker import pick_next
from app.crm.record.contracts import RawEvent
from app.crm.worker_main import ROLES

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


# --- the role ---


def test_walker_is_a_registered_role() -> None:
    assert "walker" in ROLES


def test_claim_sweeps_once_an_hour_then_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[str] = []

    async def fake_claim(batch: int) -> List[Any]:
        seen.append(f"claim:{batch}")
        return []

    async def fake_sweep() -> None:
        seen.append("sweep")

    monkeypatch.setattr(outreach_workers.walker, "claim_due_runs", fake_claim)
    monkeypatch.setattr(outreach_workers, "run_retention_sweep_tick", fake_sweep)
    monkeypatch.setattr(outreach_workers, "_last_sweep_at", float("-inf"))

    async def main() -> None:
        await outreach_workers.claim_due_runs(50)
        await outreach_workers.claim_due_runs(50)  # within the hour: no sweep

    asyncio.run(main())
    assert seen == ["sweep", "claim:50", "claim:50"]


# --- the consumer ---


def _flow(topic: str = "checkout.initiated", goal: str = "order.placed") -> Workflow:
    return Workflow(
        id=uuid.uuid4(),
        merchant_id="m1",
        name="rescue",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        draft=None,
        definition={
            "entry": {"topic": topic},
            "nodes": [{"id": "w", "type": "wait", "minutes": 30}],
            "edges": [],
            "goal": {"topics": [goal]},
        },
    )


def _event(topic: str, merchant_id: str = "m1") -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        merchant_id=merchant_id,
        source="lead-api",
        topic=topic,
        schema_version="1",
        external_id=f"{topic}:chk-1",
        payload={"customer_mobile_number": "+919845012345", "cart_value": 1999},
        received_at=NOW,
        occurred_at=NOW,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, flow: Workflow, calls: List[Any]) -> None:
    async def live_workflows(merchant_id: str) -> List[Workflow]:
        # The read is tenant-scoped in SQL: another merchant sees no plans.
        return [flow] if merchant_id == flow.merchant_id else []

    async def cancel_open_runs(*args: Any) -> int:
        calls.append(("cancel", args))
        return 1

    async def resume_run_on_event(*args: Any) -> None:
        calls.append(("resume", args))

    monkeypatch.setattr(entry.accessor, "resume_run_on_event", resume_run_on_event)

    async def fake_enrol(**kwargs: Any) -> object:
        calls.append(("enrol", kwargs))
        return object()

    monkeypatch.setattr(entry.accessor, "live_workflows", live_workflows)
    monkeypatch.setattr(entry.accessor, "cancel_open_runs", cancel_open_runs)
    monkeypatch.setattr(entry, "enrol", fake_enrol)


def test_entry_topic_enrols_with_phone_and_small_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    event = _event("checkout.initiated")
    asyncio.run(entry.consume_attributed_event(event, "cust-1"))
    ((kind, kwargs),) = calls
    assert kind == "enrol"
    assert kwargs["customer_id"] == "cust-1"
    assert kwargs["context"]["phone"] == "+919845012345"
    assert kwargs["context"]["cart_value"] == 1999
    assert kwargs["context"]["source_event_id"] == event.id


def test_goal_topic_cancels_open_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    asyncio.run(entry.consume_attributed_event(_event("order.placed"), "cust-1"))
    assert calls[0][0] == "cancel" and calls[0][1][2] == "cust-1"
    assert calls[0][1][4] == NOW  # the goal's occurred_at bounds the cancel


def test_goal_runs_before_entry_when_a_topic_is_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(topic="order.placed", goal="order.placed"), calls)
    asyncio.run(entry.consume_attributed_event(_event("order.placed"), "cust-1"))
    assert [kind for kind, _ in calls] == ["cancel", "enrol"]


def test_unmatched_topic_and_other_merchant_do_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    asyncio.run(entry.consume_attributed_event(_event("payment.refunded"), "c"))
    asyncio.run(entry.consume_attributed_event(_event("checkout.initiated", "m2"), "c"))
    assert calls == []


def test_a_failure_propagates_to_the_row_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)

    async def broken_enrol(**kwargs: Any) -> object:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(entry, "enrol", broken_enrol)
    with pytest.raises(RuntimeError):
        asyncio.run(entry.consume_attributed_event(_event("checkout.initiated"), "c"))


# --- W5: entry.where and wait_event ---

_COD_DEFINITION = {
    "entry": {"topic": "orders/create", "where": {"gateway": "COD"}},
    "nodes": [
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "confirm", "type": "wait", "minutes": 1},
        {"id": "call", "type": "wait", "minutes": 1},
    ],
    "edges": [["ask", "confirm", "YES"], ["ask", "call", "timeout"]],
    "goal": {"topics": ["order.confirmed"]},
}


def _cod_flow() -> Workflow:
    flow = _flow()
    return flow.model_copy(update={"definition": _COD_DEFINITION})


def _event_with(topic: str, payload: dict) -> RawEvent:
    return _event(topic).model_copy(update={"payload": payload})


def test_entry_where_admits_only_matching_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _cod_flow(), calls)
    asyncio.run(
        entry.consume_attributed_event(
            _event_with("orders/create", {"gateway": "prepaid"}), "c"
        )
    )
    assert calls == []
    asyncio.run(
        entry.consume_attributed_event(
            _event_with("orders/create", {"gateway": "COD"}), "c"
        )
    )
    assert calls[0][0] == "enrol"


def test_reply_wakes_the_run_standing_on_the_listening_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    flow = _cod_flow()
    _wire(monkeypatch, flow, calls)
    asyncio.run(
        entry.consume_attributed_event(
            _event_with("button.reply", {"button_id": "YES"}), "cust-1"
        )
    )
    ((kind, args),) = calls
    assert kind == "resume"
    assert args == ("m1", str(flow.id), "cust-1", "ask", {"reply_ask": "YES"})


def test_pick_next_takes_the_answer_edge_or_timeout() -> None:
    definition = WorkflowDefinition.model_validate(_COD_DEFINITION)
    ask = definition.nodes[0]
    arrows = definition.outgoing()["ask"]
    assert pick_next(ask, arrows, {"reply_ask": "YES"}) == "confirm"
    assert pick_next(ask, arrows, {}) == "call"
    assert pick_next(ask, arrows, {"reply_ask": "MAYBE"}) is None
    assert pick_next(definition.nodes[1], [("call", None)], {}) == "call"


# --- the send node proposes one manifest row ---


def test_send_variables_are_the_small_facts_only() -> None:
    context = {
        "phone": "+91…",
        "customer_mobile_number": "98450 12345",
        "source_event_id": "e1",
        "name": "Priya",
        "cart_value": 1999,
        "lead_call": "L1",
        "message_ask": "M1",
        "reply_reply": "YES",
    }
    assert send_variables(context) == {"name": "Priya", "cart_value": 1999}


# --- entry.key: what a run is about (canon T20 col 13, ruled 31 Aug 2026) ---


def _keyed_flow() -> Workflow:
    flow = _flow(topic="orders/create", goal="order.confirmed")
    definition = dict(flow.definition or {})
    definition["entry"] = {"topic": "orders/create", "key": "order_id"}
    return flow.model_copy(update={"definition": definition})


def test_entry_key_hands_the_payload_field_to_enrol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _keyed_flow(), calls)
    asyncio.run(
        entry.consume_attributed_event(
            _event_with("orders/create", {"order_id": 4501}), "cust-1"
        )
    )
    ((kind, kwargs),) = calls
    assert kind == "enrol" and kwargs["enrollment_key"] == "4501"


def test_no_entry_key_means_the_customer_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    asyncio.run(entry.consume_attributed_event(_event("checkout.initiated"), "c"))
    assert calls[0][1]["enrollment_key"] is None  # enrol() -> customer id


def test_keyed_plan_refuses_an_event_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    _wire(monkeypatch, _keyed_flow(), calls)
    for payload in ({"cart_value": 1}, {"order_id": ""}, {"order_id": None}):
        asyncio.run(
            entry.consume_attributed_event(_event_with("orders/create", payload), "c")
        )
    assert calls == []
