"""The entry-rules consumer's listening path (W5) — rollout phase 01, B1.

A wait_event reply whose payload lacks the node's key must NOT wake the
run. Written as {reply_<node>: None} with wake_at = now, the walker's
pick_next read None as "the alarm fired" and took the timeout edge at
once: any letter on the listened topic without the key ended the
listening window early and mis-routed. The fix is at the consumer — no
resume without an answer — while pick_next keeps its alarm semantics,
pinned here so the two halves cannot drift apart."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.entry as entry
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.plans import TIMEOUT
from app.crm.outreach.schemas import Workflow, WorkflowDefinition, WorkflowNode
from app.crm.outreach.walker import pick_next
from app.crm.record.schemas import RawEvent

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

# A COD-confirmation board: the first square listens for a button reply.
_LISTENING_PLAN = {
    "entry": {"topic": "orders/create"},
    "nodes": [
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "confirm", "type": "wait", "minutes": 1},
        {"id": "call", "type": "call", "template_id": "tpl-1"},
    ],
    "edges": [["ask", "confirm", "YES"], ["ask", "call", TIMEOUT]],
    "goal": {"topics": ["order.confirmed"]},
}


def _flow() -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="cod-confirm",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=_LISTENING_PLAN,
        draft=None,
    )


def _reply(payload: Dict[str, Any]) -> RawEvent:
    return RawEvent(
        id="ev-1",
        merchant_id="m1",
        source="whatsapp",
        topic="button.reply",
        schema_version="1",
        external_id="wamid-1",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


@pytest.fixture
def resumes(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, ...]]:
    """One live listening plan; every resume the consumer asks for is
    recorded. Goal-cancel must never be reached (no goal topic matches)."""
    calls: List[Tuple[Any, ...]] = []

    async def live_workflows(merchant_id: str) -> List[Workflow]:
        return [_flow()]

    async def resume_run_on_event(*args: Any) -> None:
        calls.append(args)

    async def cancel_open_runs(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("a button reply is not a goal event")

    monkeypatch.setattr(entry.accessor, "live_workflows", live_workflows)
    monkeypatch.setattr(entry.accessor, "resume_run_on_event", resume_run_on_event)
    monkeypatch.setattr(entry.accessor, "cancel_open_runs", cancel_open_runs)
    return calls


def test_a_reply_carrying_the_key_wakes_the_listening_run(
    resumes: List[Tuple[Any, ...]],
) -> None:
    asyncio.run(consume_attributed_event(_reply({"button_id": "YES"}), "c-1", {}))
    ((merchant, _workflow_id, customer, node, patch),) = resumes
    assert (merchant, customer, node) == ("m1", "c-1", "ask")
    assert patch == {"reply_ask": "YES"}


def test_a_reply_without_the_key_never_wakes_the_run(
    resumes: List[Tuple[Any, ...]],
) -> None:
    """B1: with no answer there is nothing to branch on. Resuming with
    None made the walker take the timeout edge immediately — the listening
    window ended early on an unrelated letter. The window continues; only
    the alarm may time it out."""
    asyncio.run(consume_attributed_event(_reply({"text": "hello"}), "c-1", {}))
    assert resumes == []


# --- rollout phase 06: goal tiers with a key, and the entry event's time ---

_CART_PLAN = {
    "entry": {"topic": "checkouts/update", "reenter": True, "cooldown_hours": 0},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
    "goals": [
        {
            "topics": ["orders/create"],
            "key": {"event": "cart_token", "run": "cart_token"},
            "exit_reason": "goal_met",
        },
        {"topics": ["orders/create"], "exit_reason": "converted_elsewhere"},
    ],
}


def _order(payload: Dict[str, Any]) -> RawEvent:
    return RawEvent(
        id="ev-order",
        merchant_id="m1",
        source="shopify",
        topic="orders/create",
        schema_version="1",
        external_id="orders/create:1",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


@pytest.fixture
def cancels(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, ...]]:
    calls: List[Tuple[Any, ...]] = []

    async def live_workflows(merchant_id: str) -> List[Workflow]:
        flow = _flow()
        flow.definition = _CART_PLAN
        return [flow]

    async def cancel_open_runs(*args: Any) -> int:
        calls.append((args[3], args[5] if len(args) > 5 else None))
        return 1

    async def resume_run_on_event(*args: Any) -> None:
        raise AssertionError("an order is not a reply")

    monkeypatch.setattr(entry.accessor, "live_workflows", live_workflows)
    monkeypatch.setattr(entry.accessor, "cancel_open_runs", cancel_open_runs)
    monkeypatch.setattr(entry.accessor, "resume_run_on_event", resume_run_on_event)
    return calls


def test_the_goal_cancel_stashes_the_goal_event_with_its_amount(
    cancels: List[Tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 09: the run remembers which letter ended it — and how much it
    was worth, when the payload says so as a number — so the summary can
    sum recovered revenue without re-reading the spine."""
    patches: List[Any] = []

    async def cancel_open_runs(*args: Any) -> int:
        patches.append(args[6] if len(args) > 6 else None)
        return 1

    monkeypatch.setattr(entry.accessor, "cancel_open_runs", cancel_open_runs)
    asyncio.run(
        consume_attributed_event(
            _order({"cart_token": "chk-1", "total_price": "1850.00"}), "c-1", {}
        )
    )
    assert (
        patches
        == [
            {
                "goal": {
                    "topic": "orders/create",
                    "event_id": "ev-order",
                    "amount": "1850.00",
                }
            }
        ]
        * 2
    )  # both tiers' cancels carry it; only goal_met rows are summed
    patches.clear()
    asyncio.run(
        consume_attributed_event(
            _order({"cart_token": "chk-1", "total_price": "n/a"}), "c-1", {}
        )
    )
    assert patches[0] == {"goal": {"topic": "orders/create", "event_id": "ev-order"}}


def test_an_order_carrying_the_cart_token_is_judged_keyed_first(
    cancels: List[Tuple[Any, ...]],
) -> None:
    """THIS cart recovered -> goal_met on the run keyed to it; any other
    open run of hers still ends, as converted_elsewhere (never nudge
    someone who just bought). The keyed tier runs first so the recovered
    run is already exited when the unkeyed tier sweeps."""
    asyncio.run(consume_attributed_event(_order({"cart_token": "chk-1"}), "c-1", {}))
    assert cancels == [
        ("goal_met", ("cart_token", "chk-1")),
        ("converted_elsewhere", None),
    ]


def test_an_order_without_the_key_can_only_end_runs_elsewhere(
    cancels: List[Tuple[Any, ...]],
) -> None:
    asyncio.run(consume_attributed_event(_order({"total_price": "1850.00"}), "c-1", {}))
    assert cancels == [("converted_elsewhere", None)]


def test_enrol_stamps_the_entry_events_time_and_repeats_never_move_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G7: the run remembers WHEN its founding letter happened (occurred_at,
    else received_at) under a bookkeeping key, so goals compare against the
    event's time rather than the row's insert time. A repeat (phase 00)
    refreshes facts, never the founding time — or an order placed between
    the founding checkout and a later cart update would stop counting."""
    seen: Dict[str, Any] = {}

    async def enrol(**kwargs: Any) -> None:
        seen["enrol"] = dict(kwargs["context"])
        return None

    async def apply_repeat(*args: Any) -> bool:
        seen["repeat"] = dict(args[5])
        return True

    monkeypatch.setattr(entry, "enrol", enrol)
    monkeypatch.setattr(entry, "apply_repeat", apply_repeat)
    definition = WorkflowDefinition.model_validate(
        {**_CART_PLAN, "entry": {**_CART_PLAN["entry"], "on_repeat": "refresh_latest"}}
    )
    happened = datetime(2026, 9, 3, 9, 30, tzinfo=timezone.utc)
    event = RawEvent(
        id="ev-1",
        merchant_id="m1",
        source="shopify",
        topic="checkouts/update",
        schema_version="1",
        external_id="x",
        payload={"cart_token": "chk-1", "entered_event_at": "forged"},
        received_at=NOW,
        occurred_at=happened,
    )
    asyncio.run(entry._try_enrol(_flow(), definition, event, "c-1", {}))
    assert seen["enrol"]["entered_event_at"] == happened.isoformat()
    assert "entered_event_at" not in seen["repeat"]
    assert "source_event_id" not in seen["repeat"]
    assert seen["repeat"]["cart_token"] == "chk-1"


def test_the_alarm_path_still_times_a_listening_node_out() -> None:
    """pick_next keeps its law: no answer in context = the alarm fired ->
    the timeout edge. B1 is fixed at the consumer, never here."""
    node = WorkflowNode(
        id="ask",
        type="wait_event",
        topics=["button.reply"],
        key="button_id",
        minutes=60,
    )
    arrows: List[Tuple[str, Optional[str]]] = [("confirm", "YES"), ("call", TIMEOUT)]
    assert pick_next(node, arrows, {}) == "call"
    assert pick_next(node, arrows, {"reply_ask": "YES"}) == "confirm"
