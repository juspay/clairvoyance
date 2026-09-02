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
from app.crm.outreach.schemas import Workflow, WorkflowNode
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
