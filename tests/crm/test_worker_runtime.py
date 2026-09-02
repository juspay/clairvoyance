"""The walker role: its registration, the hourly sweep riding its claim; and
the entry-rules consumer: one row in,
goal before entry, unmatched topics ignored, and a failure that propagates
(the event worker's savepoint is what isolates it — never a swallow here)."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.workers as outreach_workers
from app.crm.outreach.db.accessors import version as version_accessor
from app.crm.outreach.nodes import send_variables
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition
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


def _open_run(flow: Workflow) -> EnrollmentRun:
    """One open run of hers on the plan's first square, pinned to the
    version the live row carries (phase 13: goals and listening are judged
    per open run, by its own version)."""
    first_node = (flow.definition or {})["nodes"][0]["id"]
    return EnrollmentRun(
        id=uuid.uuid4(),
        merchant_id=flow.merchant_id,
        workflow_id=flow.id,
        workflow_version=flow.version,
        customer_id=uuid.uuid4(),
        status="waiting",
        current_node=first_node,
        wake_at=NOW + timedelta(minutes=30),
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919845012345"},
        enrollment_key="cust-1",
        attempts=0,
        last_error=None,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, flow: Workflow, calls: List[Any]
) -> EnrollmentRun:
    run = _open_run(flow)
    definitions._definitions.clear()

    async def live_workflows(merchant_id: str) -> List[Workflow]:
        # The read is tenant-scoped in SQL: another merchant sees no plans.
        return [flow] if merchant_id == flow.merchant_id else []

    async def open_runs_for_customer(
        merchant_id: str, customer_id: str
    ) -> List[EnrollmentRun]:
        return [run] if merchant_id == flow.merchant_id else []

    async def get_definition(
        merchant_id: str, workflow_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        return (
            flow.definition
            if (workflow_id, version) == (str(flow.id), flow.version)
            else None
        )

    async def cancel_run(*args: Any) -> bool:
        calls.append(("cancel", args))
        return True

    async def resume_run_by_id(*args: Any) -> bool:
        calls.append(("resume", args))
        return True

    async def fake_enrol(**kwargs: Any) -> object:
        calls.append(("enrol", kwargs))
        return object()

    monkeypatch.setattr(entry.workflow_accessor, "live_workflows", live_workflows)
    monkeypatch.setattr(
        entry.enrollment_accessor, "open_runs_for_customer", open_runs_for_customer
    )
    monkeypatch.setattr(version_accessor, "get_definition", get_definition)
    monkeypatch.setattr(entry.enrollment_accessor, "cancel_run", cancel_run)
    monkeypatch.setattr(entry.enrollment_accessor, "resume_run_by_id", resume_run_by_id)
    monkeypatch.setattr(entry, "enrol", fake_enrol)
    return run


def test_declared_variables_ride_into_the_run_context_and_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine resolved the catalog's variable fields (nested paths,
    derived names) — they land in context beside the scalar copy and
    override it; a bookkeeping name in the variables is still ours."""
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    event = _event("checkout.initiated")
    asyncio.run(
        entry.consume_attributed_event(
            event,
            "cust-1",
            variables={
                "customer_name": "Priya Sharma",
                "cart_value": 2999,
                "source_event_id": "forged",
            },
        )
    )
    ((kind, kwargs),) = calls
    assert kind == "enrol"
    assert kwargs["context"]["customer_name"] == "Priya Sharma"
    assert kwargs["context"]["cart_value"] == 2999  # declared beats the scalar copy
    assert kwargs["context"]["source_event_id"] == event.id


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


def test_extractor_handles_reach_context_when_the_payload_hides_the_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parked-run regression: a verbatim Shopify letter carries its
    # phone ONLY in customer.default_address — none of this file's
    # fallback paths. Before handles were passed through, this order
    # resolved, enrolled, and parked at its first call/send node
    # ("no phone in run context"). The extractor's discovery must win.
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    event = _event("checkout.initiated")
    event = event.model_copy(
        update={
            "source": "shopify",
            "payload": {
                "customer": {
                    "phone": None,
                    "default_address": {"phone": "+91 98450 12345"},
                },
                "cart_value": 1999,
            },
        }
    )
    asyncio.run(
        entry.consume_attributed_event(
            event, "cust-1", handles={"phone": "+919845012345"}
        )
    )
    ((kind, kwargs),) = calls
    assert kind == "enrol"
    assert kwargs["context"]["phone"] == "+919845012345"


def test_extractor_handles_beat_the_payload_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Precedence pin: when both exist, the extractor's handle wins — the
    # number the sends dial must be the number identity resolved on, or
    # suppression stops matching what we contact.
    calls: List[Any] = []
    _wire(monkeypatch, _flow(), calls)
    event = _event("checkout.initiated")  # payload carries +919845012345
    asyncio.run(
        entry.consume_attributed_event(
            event, "cust-1", handles={"phone": "+918888877777"}
        )
    )
    ((kind, kwargs),) = calls
    assert kind == "enrol"
    assert kwargs["context"]["phone"] == "+918888877777"


def test_goal_topic_cancels_open_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    run = _wire(monkeypatch, _flow(), calls)
    asyncio.run(entry.consume_attributed_event(_event("order.placed"), "cust-1"))
    assert calls[0][0] == "cancel" and calls[0][1][1] == str(run.id)
    assert calls[0][1][3] == NOW  # the goal's occurred_at bounds the cancel


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
    "entry": {
        "topic": "orders/create",
        "where": [{"field": "payload.gateway", "op": "is", "value": "COD"}],
    },
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
    run = _wire(monkeypatch, _cod_flow(), calls)
    asyncio.run(
        entry.consume_attributed_event(
            _event_with("button.reply", {"button_id": "YES"}), "cust-1"
        )
    )
    ((kind, args),) = calls
    assert kind == "resume"
    assert args == (
        "m1",
        str(run.id),
        "ask",
        {"reply_ask": "YES", "latest_letter": "ask"},
        {"button_id": "YES"},
    )


def test_pick_next_takes_the_answer_edge_or_timeout() -> None:
    definition = WorkflowDefinition.model_validate(_COD_DEFINITION)
    ask = definition.nodes[0]
    arrows = definition.outgoing()["ask"]
    assert pick_next(ask, arrows, {"reply_ask": "YES"}) == "confirm"
    assert pick_next(ask, arrows, {}) == "call"
    assert pick_next(ask, arrows, {"reply_ask": "MAYBE"}) is None
    assert pick_next(definition.nodes[1], [("call", None)], {}) == "call"


# --- the send node proposes one manifest row ---


def test_send_variables_are_only_the_mapped_facts() -> None:
    """A template with two blanks handed 27 facts is refused by every
    provider (the COD demo: 'confirmed' is bool) — the send node names
    which fact fills which blank, and an unmapped template posts none."""
    context = {
        "phone": "+91…",
        "customer_mobile_number": "98450 12345",
        "source_event_id": "e1",
        "customer_name": "Priya",
        "name": "#1001",
        "cart_value": 1999,
        "confirmed": True,
        "lead_call": "L1",
        "message_ask": "M1",
        "reply_reply": "YES",
    }
    assert send_variables(
        {"customer_name": "customer_name", "order_no": "name"}, context
    ) == {"customer_name": "Priya", "order_no": "#1001"}
    assert send_variables({}, context) == {}  # hello_world: zero parameters


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
