"""Outreach hygiene (enh A/06): the dedupe id reaches the run, the
validator warns on what is legal and probably not meant, and the entry
consumer hunts nothing the extractor already found.

N12 — a lease retry's absorbed send still writes message_<node>.
N15 — an edge label no rule or arm answers is warned about, not refused.
N16 — warnings ride the create/draft/publish answers beside problems.
N14 is pinned where it lived (test_workflow_admission, test_workflow_simulate).
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

import app.crm.outreach.nodes.send as send_mod
import app.crm.outreach.plans as plans_mod
from app.crm.outreach.plans import definition_warnings, validate_definition
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
NOW = "2026-09-10T12:00:00+00:00"


def _plan(**over: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "entry": {"topic": "orders/create"},
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "msg",
                "type": "send",
                "channel": "whatsapp",
                "template": "t",
                "variables": {},
            },
        ],
        "edges": [["wait-30m", "msg"]],
        "goals": [{"topics": ["orders/paid"]}],
        "exits": {"max_age_days": 7},
        "purpose_key": "utility.order.cod_confirm",
    }
    doc.update(over)
    return doc


# --- N12 ---------------------------------------------------------------------


def _run() -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="msg",
        wake_at=None,
        entered_at=NOW,
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919000000001", "customer_name": "Priya"},
        enrollment_key="k",
        attempts=0,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_an_absorbed_send_still_writes_the_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listening square after a send matches letters on message_<node>
    (phase 18). A lease retry whose insert was absorbed used to write
    nothing, leaving that square deaf to its own receipt."""
    asked: List[Any] = []

    async def _absorbed(**kwargs: Any) -> Optional[str]:
        return None  # the dedupe unique took it

    async def _lookup(merchant_id: str, dedupe_key: str) -> Optional[str]:
        asked.append((merchant_id, dedupe_key))
        return "msg-42"

    monkeypatch.setattr(send_mod, "queue_message", _absorbed)
    monkeypatch.setattr(send_mod, "message_id_for_dedupe", _lookup)

    run = _run()
    definition = WorkflowDefinition.model_validate(_plan())
    node = next(n for n in definition.nodes if n.id == "msg")
    written = await send_mod.execute(run, node, definition)

    assert written == {"message_msg": "msg-42"}
    assert asked == [("m1", f"{run.id}:msg")]


@pytest.mark.asyncio
async def test_a_first_send_never_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup is the retry's cost only; a fresh send has its id."""

    async def _queued(**kwargs: Any) -> Optional[str]:
        return "msg-1"

    async def _never(merchant_id: str, dedupe_key: str) -> Optional[str]:
        raise AssertionError("a fresh send must not look itself up")

    monkeypatch.setattr(send_mod, "queue_message", _queued)
    monkeypatch.setattr(send_mod, "message_id_for_dedupe", _never)
    definition = WorkflowDefinition.model_validate(_plan())
    node = next(n for n in definition.nodes if n.id == "msg")
    assert await send_mod.execute(_run(), node, definition) == {"message_msg": "msg-1"}


@pytest.mark.asyncio
async def test_an_absorbed_send_with_no_row_writes_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Total: the lookup can come back empty (a row deleted between the
    two attempts). The run carries on without the id rather than parking
    on a send that did, in fact, happen."""

    async def _absorbed(**kwargs: Any) -> Optional[str]:
        return None

    async def _gone(merchant_id: str, dedupe_key: str) -> Optional[str]:
        return None

    monkeypatch.setattr(send_mod, "queue_message", _absorbed)
    monkeypatch.setattr(send_mod, "message_id_for_dedupe", _gone)
    definition = WorkflowDefinition.model_validate(_plan())
    node = next(n for n in definition.nodes if n.id == "msg")
    assert await send_mod.execute(_run(), node, definition) == {}


# --- N16: the four readings -------------------------------------------------


def test_a_square_nothing_leads_to_is_warned_not_refused() -> None:
    """The bug this exists for: five complete squares, no arrows, saved
    and published cleanly, ran the first one and stopped."""
    doc = _plan(edges=[])
    assert validate_definition(doc) == []  # legal
    assert definition_warnings(doc) == [
        "nothing leads to msg — no door starts there and no arrow reaches it, "
        "so it never runs"
    ]


def test_a_door_that_starts_mid_board_counts_as_reached() -> None:
    """A multi-door plan admits people onto mid-board squares; seeding
    from nodes[0] alone would warn on every one of them."""
    doc = _plan(
        entry=[
            {"topic": "orders/create", "start": "wait-30m"},
            {"topic": "orders/paid", "start": "msg"},
        ],
        edges=[],
        goals=[{"topics": ["orders/cancelled"]}],
    )
    assert definition_warnings(doc) == []


def test_a_loop_is_named_once_and_stays_legal() -> None:
    """The Flipkart nudge re-arms on every letter by design; the warning
    says what ends a run inside it, and refuses nothing."""
    doc = _plan(
        nodes=[
            {"id": "quiet", "type": "wait", "minutes": 5},
            {"id": "nudge", "type": "call", "template_id": "t"},
            {
                "id": "listen",
                "type": "wait_event",
                "topics": ["OFFERED"],
                "key": "$topic",
                "minutes": 60,
            },
        ],
        edges=[["quiet", "nudge"], ["nudge", "listen"], ["listen", "quiet", "OFFERED"]],
    )
    assert validate_definition(doc) == []
    warnings = definition_warnings(doc)
    assert [w for w in warnings if "loops back" in w] == [
        "the board loops back on itself — fine when meant (a nudge that re-arms "
        "on every letter); a run inside the loop ends only by a goal, a timeout "
        "edge, or the plan's max age"
    ]


def test_a_listening_square_with_no_timeout_edge_is_warned() -> None:
    doc = _plan(
        nodes=[
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "reply",
                "type": "wait_event",
                "topics": ["message.inbound"],
                "key": "reply",
                "minutes": 60,
            },
            {"id": "done", "type": "wait", "minutes": 1},
        ],
        edges=[["wait-30m", "reply"], ["reply", "done", "CONFIRM"]],
    )
    assert validate_definition(doc) == []
    assert definition_warnings(doc) == [
        "reply: listens with no 'timeout' or 'else' edge — when the alarm wins, "
        "the run ends there"
    ]


# --- N15: a label no square answers -----------------------------------------


def test_a_condition_edge_no_rule_answers_is_warned() -> None:
    doc = _plan(
        nodes=[
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "decide",
                "type": "condition",
                "rules": [
                    {
                        "on": "big",
                        "if": [{"field": "context.total", "op": ">=", "value": 5}],
                    }
                ],
            },
            {"id": "a", "type": "wait", "minutes": 1},
            {"id": "b", "type": "wait", "minutes": 1},
            {"id": "c", "type": "wait", "minutes": 1},
        ],
        edges=[
            ["wait-30m", "decide"],
            ["decide", "a", "big"],
            ["decide", "b", "else"],
            ["decide", "c", "huge"],  # no rule says "huge"
        ],
    )
    assert validate_definition(doc) == []  # a stray label is legal
    assert definition_warnings(doc) == [
        "decide: edge labelled 'huge' — no rule or arm of this square answers "
        "that, so the edge is never taken"
    ]


def test_a_split_edge_no_arm_answers_is_warned() -> None:
    doc = _plan(
        nodes=[
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "which",
                "type": "split",
                "arms": [{"on": "A", "percent": 50}, {"on": "B", "percent": 50}],
            },
            {"id": "a", "type": "wait", "minutes": 1},
            {"id": "b", "type": "wait", "minutes": 1},
            {"id": "z", "type": "wait", "minutes": 1},
        ],
        edges=[
            ["wait-30m", "which"],
            ["which", "a", "A"],
            ["which", "b", "B"],
            ["which", "z", "timeout"],  # a split never times out
        ],
    )
    assert validate_definition(doc) == []
    assert definition_warnings(doc) == [
        "which: edge labelled 'timeout' — no rule or arm of this square answers "
        "that, so the edge is never taken"
    ]


def test_a_listening_square_may_carry_any_label() -> None:
    """Its answers come from letters nobody can enumerate at publish; a
    label there is a promise about the letter, not a dead edge."""
    doc = _plan(
        nodes=[
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "reply",
                "type": "wait_event",
                "topics": ["message.inbound"],
                "key": "reply",
                "minutes": 60,
            },
            {"id": "a", "type": "wait", "minutes": 1},
            {"id": "b", "type": "wait", "minutes": 1},
        ],
        edges=[
            ["wait-30m", "reply"],
            ["reply", "a", "ANYTHING_AT_ALL"],
            ["reply", "b", "timeout"],
        ],
    )
    assert definition_warnings(doc) == []


# --- the shipped boards are clean, and problems are not warnings -------------


@pytest.mark.parametrize("path", sorted(PLANS.glob("*.json")), ids=lambda p: p.stem)
def test_no_shipped_board_carries_dead_structure(path: Path) -> None:
    """Two of the four readings are DEAD structure — a square nothing
    leads to, a label nothing answers — and a template we hand merchants
    must carry neither. The other two are "did you mean this" (a loop, a
    listening window that ends the run), and the loan ladder means both:
    every stage's after-window ends the journey by design (phase 17)."""
    doc = json.loads(path.read_text())
    dead = [
        w
        for w in definition_warnings(doc)
        if "nothing leads to" in w or "never taken" in w
    ]
    assert dead == [], (path.name, dead)


def test_the_loan_ladder_names_its_deaf_windows_once() -> None:
    """The ladder gives every after-window arrows to later stages and no
    timeout edge — the alarm ending the journey IS the design. One line
    names all of them; four copies would teach an author to stop
    reading."""
    doc = json.loads((PLANS / "loan-dropoff.json").read_text())
    deaf = [w for w in definition_warnings(doc) if "listens with no" in w]
    assert len(deaf) == 1
    assert deaf[0].startswith("after-profile-created, after-kyc-completed, ")


def test_a_document_with_problems_has_no_warnings() -> None:
    """Two lists that disagree are worse than one; a document that does
    not validate is reported by validate_definition alone."""
    doc = _plan(edges=[["wait-30m", "nowhere"]])
    assert validate_definition(doc) != []
    assert definition_warnings(doc) == []


# --- N16: they ride the write answers ---------------------------------------


@pytest.mark.asyncio
async def test_create_and_draft_answers_carry_the_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from app.crm.outreach.schemas import Workflow

    stored = Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="p",
        status="draft",
        version=0,
        created_by=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        definition=None,
        draft=_plan(edges=[]),
    )

    async def _gather(merchant_id: str, raw: Dict[str, Any]) -> Any:
        return None

    async def _insert(*args: Any, **kwargs: Any) -> Workflow:
        return stored

    async def _update(*args: Any, **kwargs: Any) -> Workflow:
        return stored

    monkeypatch.setattr(plans_mod, "_gather_catalogs", _gather)
    monkeypatch.setattr(plans_mod.workflow_accessor, "insert_workflow", _insert)
    monkeypatch.setattr(plans_mod.workflow_accessor, "update_draft", _update)

    created = await plans_mod.create_workflow("m1", "p", _plan(edges=[]), None)
    drafted = await plans_mod.update_draft("m1", str(stored.id), _plan(edges=[]))
    for answer in (created, drafted):
        assert answer is not None
        assert answer.warnings == [
            "nothing leads to msg — no door starts there and no arrow reaches it, "
            "so it never runs"
        ]
    # never stored: the row the accessor returned is untouched
    assert stored.warnings == []
