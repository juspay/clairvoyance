"""The dry run (enh A/05): walk a board against a sample letter, with a
fake clock and no writes.

The boards walked here are the SHIPPED plan templates, not fixtures
invented for the test — if a dry run cannot walk the documents we hand
merchants, it cannot walk theirs. Every assertion is about what an author
would read: who got in, what fired, when, and where the run ended.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.routing import APIRoute

import app.crm.outreach.api as outreach_api
import app.crm.outreach.simulate as simulate_mod
from app.crm.auth import crm_admin_user
from app.crm.outreach.nodes import NODE_TYPES
from app.crm.outreach.schemas import (
    SimulateEvent,
    SimulateRequest,
    SimulateResult,
    Workflow,
    WorkflowDefinition,
)
from app.crm.outreach.simulate import MAX_STEPS, SimulationRefused, _pick, simulate
from app.crm.outreach.walker import pick_next
from app.crm.record.catalog import code_entries

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
CART = json.loads((PLANS / "cart-recovery.json").read_text())
TIERED = json.loads((PLANS / "cart-recovery-tiered.json").read_text())
SPLIT = json.loads((PLANS / "cart-recovery-split.json").read_text())

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

CHECKOUT = {
    "id": 7115774787859,
    "cart_token": "chk-1",
    "customer_mobile_number": "+919000000001",
    "customer_name": "Priya",
    "total_price": "1200.00",
}


def _catalogs() -> Dict[str, Optional[Dict[str, Any]]]:
    return {entry.topic: {f.path: f for f in entry.fields} for entry in code_entries()}


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch):
    """One live plan, its document swapped per test. The only two reads a
    dry run makes are stubbed here: the plan row and the catalog."""

    def _install(document: Dict[str, Any], draft: Optional[Dict[str, Any]] = None):
        workflow = Workflow(
            id="11111111-1111-4111-8111-111111111111",
            merchant_id="m1",
            name="cart",
            status="live",
            version=1,
            definition=document,
            draft=draft,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
        )

        async def _get(merchant_id: str, workflow_id: str):
            return workflow if merchant_id == "m1" else None

        async def _gather(merchant_id: str, raw: Dict[str, Any]):
            return _catalogs()

        monkeypatch.setattr(
            simulate_mod.workflow_accessor, "get_workflow", _get, raising=False
        )
        monkeypatch.setattr(simulate_mod, "gather_catalogs", _gather)
        return workflow

    return _install


WF = "11111111-1111-4111-8111-111111111111"


async def _walk(request: SimulateRequest, merchant: str = "m1") -> SimulateResult:
    """The dry run, with "the plan is there" asserted once — every test
    below is about what the walk SAYS, not about whether it happened."""
    result = await simulate(merchant, WF, request)
    assert result is not None
    return result


def _request(**over: Any) -> SimulateRequest:
    body: Dict[str, Any] = {
        "event": SimulateEvent(
            topic="checkouts/update", payload=dict(CHECKOUT), source="shopify"
        )
    }
    body.update(over)
    return SimulateRequest(**body)


# --- the walk ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cart_board_walks_to_the_end_with_its_offsets(plan) -> None:
    """The shipped cart board, no answers supplied: wait, WhatsApp, wait,
    call, wait, done — and the offsets an author drew."""
    plan(CART)
    result = await _walk(_request())

    assert result is not None and result.admitted and result.reason == "admitted"
    assert [(s.node, s.type, s.at) for s in result.path] == [
        ("wait-30m", "wait", "+00:00"),
        ("wa-nudge", "send", "+00:30"),
        ("wait-30m-2", "wait", "+00:30"),
        ("rescue-call", "call", "+01:00"),
        ("wait-1d", "wait", "+01:00"),
    ]
    assert result.exit is not None
    assert (result.exit.reason, result.exit.at) == ("completed", "+25:00")


@pytest.mark.asyncio
async def test_a_send_says_what_it_would_post_without_posting(plan) -> None:
    """The whole point: the message, resolved, never sent."""
    plan(CART)
    result = await _walk(_request())

    send = next(s for s in result.path if s.type == "send")
    assert send.action == {
        "channel": "whatsapp",
        "template": "cart_recovery_1",
        "variables": {"1": "Priya"},
    }
    call = next(s for s in result.path if s.type == "call")
    assert call.action is not None
    assert call.action["payload"]["customer_mobile_number"] == "+919000000001"
    assert result.problems == []


@pytest.mark.asyncio
async def test_a_send_that_would_park_is_reported_not_raised(plan) -> None:
    """A letter with no phone: the board still walks, and the park the
    author would have met on a real customer is named."""
    plan(CART)
    payload = {k: v for k, v in CHECKOUT.items() if k != "customer_mobile_number"}
    result = await _walk(
        _request(event=SimulateEvent(topic="checkouts/update", payload=payload))
    )
    assert result.admitted and len(result.path) == 5
    assert result.problems == [
        "wa-nudge: no phone in the letter — this send would park",
        "rescue-call: no phone in the letter — this call would park",
    ]


# --- the branching squares, evaluated for real -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cart_value,expected",
    [(6000, "rescue-call"), (1200, "wa-nudge")],
)
async def test_the_tiered_board_takes_the_arm_the_facts_choose(
    plan, cart_value: int, expected: str
) -> None:
    """`facts` is how an author asks "what if the cart were 6,000" without
    editing the payload — the condition then evaluates for real."""
    plan(TIERED)
    result = await _walk(_request(facts={"total_price": cart_value}))
    assert [s.node for s in result.path][:3] == ["wait-30m", "decide", expected]
    decide = next(s for s in result.path if s.node == "decide")
    assert decide.answer == ("big" if expected == "rescue-call" else None)


@pytest.mark.asyncio
async def test_the_split_board_gives_the_same_arm_every_time(plan) -> None:
    """A dry run feeds the split a fixed id, so pressing the button twice
    cannot show two different boards — the coin is real for real runs."""
    plan(SPLIT)
    first = await _walk(_request())
    again = await _walk(_request())

    arm = next(s for s in first.path if s.type == "split").answer
    assert arm in {"control", "variant"}
    assert [s.node for s in first.path] == [s.node for s in again.path]
    assert [s.node for s in first.path][:3] == [
        "wait-30m",
        "which-letter",
        f"wa-{arm}",
    ]


# --- the listening square ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_listening_square_times_out_and_costs_its_window(
    plan,
) -> None:
    """No answer supplied = the alarm won, so the window's minutes pass and
    the `timeout` arrow is taken."""
    fallback = json.loads((PLANS / "cart-recovery-fallback.json").read_text())
    plan(fallback)
    result = await _walk(_request())

    listening = next(s for s in result.path if s.type == "wait_event")
    assert listening.answer is None and listening.at == "+01:00"
    after = result.path[result.path.index(listening) + 1]
    assert after.node == "wait-1d" and after.at == "+25:00"


@pytest.mark.asyncio
async def test_an_answered_listening_square_costs_nothing_and_takes_its_label(
    plan,
) -> None:
    """Supplying a reply says it arrived: what follows happens then, not a
    day later."""
    fallback = json.loads((PLANS / "cart-recovery-fallback.json").read_text())
    plan(fallback)
    result = await _walk(_request(answers={"after-call": "NO_ANSWER"}))
    listening = next(s for s in result.path if s.type == "wait_event")
    after = result.path[result.path.index(listening) + 1]
    assert listening.answer == "NO_ANSWER"
    assert after.node == "wa-fallback" and after.at == listening.at


# --- refusals ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_letter_on_another_topic_is_not_admitted(plan) -> None:
    plan(CART)
    result = await _walk(_request(event=SimulateEvent(topic="orders/paid", payload={})))
    assert result.admitted is False
    assert result.reason == "no door on this plan starts on 'orders/paid'"
    assert result.path == []


@pytest.mark.asyncio
async def test_a_document_that_would_not_publish_is_refused_with_its_problems(
    plan,
) -> None:
    """The publish validator's own list, so an author fixes one thing
    rather than two."""
    broken = {**CART, "nodes": [*CART["nodes"]], "edges": [["wait-30m", "nowhere"]]}
    plan(broken)
    with pytest.raises(SimulationRefused) as raised:
        await simulate("m1", WF, _request())
    assert any("nowhere" in p for p in raised.value.problems)


@pytest.mark.asyncio
async def test_another_merchants_plan_is_not_there(plan) -> None:
    plan(CART)
    assert (
        await simulate("m2", "11111111-1111-4111-8111-111111111111", _request()) is None
    )


@pytest.mark.asyncio
async def test_a_looping_board_stops_and_says_so(plan) -> None:
    """A board that never ends must not hang a request."""
    looping = {
        **CART,
        "nodes": [{"id": "spin", "type": "wait", "minutes": 1}],
        "edges": [["spin", "spin"]],
        "exits": {"max_age_days": 3650},
    }
    plan(looping)
    result = await _walk(_request())
    assert len(result.path) == MAX_STEPS and result.exit is None
    assert any("still walking" in p for p in result.problems)


# --- the one rule that must not be written twice -----------------------------


def test_the_dry_run_picks_the_same_arrow_the_walker_would() -> None:
    """Two rules for one board would be two boards. Every shape the
    walker's pick_next answers, this one answers identically."""
    node = WorkflowDefinition(
        **{
            "entry": {"topic": "t"},
            "nodes": [
                {
                    "id": "listen",
                    "type": "wait_event",
                    "topics": ["t"],
                    "key": "reply",
                    "minutes": 10,
                }
            ],
            "edges": [],
            "goals": [{"topics": ["g"]}],
            "exits": {"max_age_days": 1},
        }
    ).nodes[0]
    arrows: List[Any] = [("a", "YES"), ("b", "timeout"), ("c", "else")]
    for answer in ("YES", "NOPE", None):
        context = {} if answer is None else {"reply_listen": answer}
        assert _pick(node, arrows, answer) == pick_next(node, arrows, context)


def test_only_the_squares_that_fire_describe_themselves() -> None:
    """A dry run asks the registry what a square would do; a square that
    only waits has nothing to say, and one that decides for itself answers
    through `decide` instead."""
    assert {w for w, s in NODE_TYPES.items() if s.describe} == {
        "send",
        "call",
        "action",
    }
    assert {w for w, s in NODE_TYPES.items() if s.decide} == {"condition", "split"}


def test_the_route_is_admin_only_and_mounted() -> None:
    route = next(
        r
        for r in outreach_api.router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/simulate")
    )
    assert "POST" in route.methods
    assert any(
        getattr(d, "call", None) is crm_admin_user for d in route.dependant.dependencies
    ), "the dry run reads a plan and must stay admin-only"
