"""The plan templates under docs/crm/plans/ are real documents, validated
on every CI run (rollout phase 07): the cart-recovery board (§16.1) and,
as of phase 17, the loan-dropoff funnel as ONE pinned board written as a
`stages` ladder (§16.2) — five clocks folded into one document.

A document that stops validating fails CI; a loan board whose expansion
does not give every stage one labelled arrow to every later stage fails
CI — one missing arrow is one wrong phone call (§14.3 objection 5)."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from app.crm.outreach.ladder import expand_stages
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import WorkflowDefinition

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
CART = PLANS / "cart-recovery.json"
LOAN = PLANS / "loan-dropoff.json"

# The funnel, in order (§16.2): stage i listens for every stage after it;
# disbursed ends the journey as the goal, rejected/withdrawn as withdrawn.
LOAN_STAGES = [
    "loan.profile_created",
    "loan.kyc_completed",
    "loan.bank_linked",
    "loan.offer_accepted",
    "loan.agreement_signed",
]
LOAN_DONE = "loan.disbursed"
LOAN_OUT = ["loan.rejected", "loan.withdrawn"]


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _every_plan() -> List[Path]:
    return sorted(PLANS.rglob("*.json"))


def _slug(topic: str) -> str:
    return topic.split(".")[-1].replace("_", "-")


def test_the_expected_documents_exist() -> None:
    assert CART.is_file(), CART
    assert LOAN.is_file(), LOAN
    assert _every_plan() == [CART, LOAN]


@pytest.mark.parametrize("path", _every_plan(), ids=lambda p: p.stem)
def test_every_plan_template_validates(path: Path) -> None:
    assert validate_definition(_load(path)) == [], path


def test_cart_recovery_is_the_final_shape_from_the_notes() -> None:
    doc = _load(CART)
    entry = doc["entry"]
    assert entry["topic"] == "checkouts/update"
    assert entry["reenter"] is True and entry["cooldown_hours"] == 24
    assert entry["on_repeat"] == "refresh_latest" and entry["debounce_minutes"] == 30
    assert doc["purpose_key"] == "marketing.cart.recovery"
    assert doc["exits"] == {"max_age_days": 7}
    # two tiers: THIS cart recovered, then anything else she bought
    recovered, elsewhere = doc["goals"]
    assert recovered["key"] == {"event": "cart_token", "run": "cart_token"}
    assert recovered["exit_reason"] == "goal_met"
    assert "key" not in elsewhere and elsewhere["exit_reason"] == "converted_elsewhere"
    assert (
        recovered["topics"] == elsewhere["topics"] == ["orders/create", "orders/paid"]
    )
    # wait 30 -> WhatsApp -> wait 30 -> call -> wait 1d -> completed
    assert [(n["type"], n.get("minutes")) for n in doc["nodes"]] == [
        ("wait", 30),
        ("send", None),
        ("wait", 30),
        ("call", None),
        ("wait", 1440),
    ]
    send = doc["nodes"][1]
    assert send["channel"] == "whatsapp" and send["template"] == "cart_recovery_1"
    assert doc["nodes"][3]["template_id"] == "TEMPLATE_ID_PLACEHOLDER"
    ids = [n["id"] for n in doc["nodes"]]
    assert doc["edges"] == [[a, b] for a, b in zip(ids, ids[1:])]


def test_loan_dropoff_is_one_pinned_board_written_as_a_ladder() -> None:
    doc = _load(LOAN)
    assert doc["on_publish"] == "pin"  # journeys live weeks: a fix never moves them
    assert doc["key"] == "application_id"
    # an hour, as the clocks had: a stage letter delivered late, after the
    # journey ended, must not open a new one and call
    assert doc["reenter"] is True and doc["cooldown_hours"] == 1
    assert doc["on_repeat"] == "refresh_latest"
    assert doc["exits"] == {"max_age_days": 30}
    assert doc["goals"] == [
        {"topics": [LOAN_DONE], "exit_reason": "goal_met"},
        {"topics": LOAN_OUT, "exit_reason": "withdrawn"},
    ]
    stages = doc["stages"]
    assert stages["order"] == LOAN_STAGES
    assert stages["idle_minutes"] == 30 and stages["after_action_minutes"] == 1440
    assert stages["on_idle"] == {
        "type": "call",
        "template_id": "TEMPLATE_ID_PLACEHOLDER",
    }
    assert stages["restart_on_repeat"] is True
    assert stages["overrides"] == {"loan.offer_accepted": {"idle_minutes": 120}}
    # the ladder is the whole board: nothing hand-drawn beside it
    assert not {"nodes", "edges", "entry"} & set(doc)


def test_the_loan_board_has_one_arrow_from_every_stage_to_every_later_one() -> None:
    """The guard the clocks had as a goal-list check, now on the board:
    the expansion's edge set must equal the set computed from the ordered
    funnel, and every listening square must name exactly its downstream."""
    definition = WorkflowDefinition.model_validate(expand_stages(_load(LOAN)))
    by_id = {node.id: node for node in definition.nodes}
    expected: Set[Tuple[str, str, Optional[str]]] = set()
    for index, topic in enumerate(LOAN_STAGES):
        at, act, after = (f"{p}-{_slug(topic)}" for p in ("at", "act", "after"))
        downstream = LOAN_STAGES[index + 1 :]
        if not downstream:
            expected.add((at, act, None))
            assert by_id[at].type == "wait" and after not in by_id
            continue
        for later in downstream:
            expected.add((at, f"at-{_slug(later)}", later))
            expected.add((after, f"at-{_slug(later)}", later))
        expected.add((at, act, "timeout"))
        expected.add((act, after, None))
        assert by_id[at].topics == by_id[after].topics == downstream, topic
    actual = {(e[0], e[1], e[2] if len(e) == 3 else None) for e in definition.edges}
    assert actual == expected
    assert len(definition.edges) == len(actual)  # no arrow twice
    assert [(d.topic, d.start) for d in definition.entries] == [
        (t, f"at-{_slug(t)}") for t in LOAN_STAGES
    ]
    assert all(n.stage in LOAN_STAGES for n in definition.nodes)
    # the offer stage waits longer, the others keep the ladder's clock
    assert by_id["at-offer-accepted"].minutes == 120
    assert by_id["at-kyc-completed"].minutes == 30
