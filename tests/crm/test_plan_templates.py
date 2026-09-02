"""The plan templates under docs/crm/plans/ are real documents, validated
on every CI run (rollout phase 07): the cart-recovery board (§16.1) and
the loan-dropoff funnel as per-stage clocks (§13 Option A) — the shape
the funnel ships in until phase 17 folds it into one pinned board.

A document that stops validating fails CI; a loan stage plan whose goal
list is not exactly "every downstream stage, plus disbursed" fails CI —
one missing topic is one wrong phone call (§14.3 objection 5)."""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.crm.outreach.plans import validate_definition

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
CART = PLANS / "cart-recovery.json"
LOAN = PLANS / "loan-dropoff"

# The funnel, in order (§16.2). The goal of stage i is every stage after
# it plus the terminal topic; rejected/withdrawn is the second tier.
LOAN_STAGES = [
    ("01-profile", "loan.profile_created"),
    ("02-kyc", "loan.kyc_completed"),
    ("03-bank", "loan.bank_linked"),
    ("04-offer", "loan.offer_accepted"),
    ("05-agreement", "loan.agreement_signed"),
]
LOAN_DONE = "loan.disbursed"
LOAN_OUT = ["loan.rejected", "loan.withdrawn"]


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _every_plan() -> List[Path]:
    return sorted(PLANS.rglob("*.json"))


def test_the_expected_documents_exist() -> None:
    assert CART.is_file(), CART
    for stem, _ in LOAN_STAGES:
        assert (LOAN / f"{stem}.json").is_file(), stem
    assert len(_every_plan()) == 1 + len(LOAN_STAGES)


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


@pytest.mark.parametrize(
    "index", range(len(LOAN_STAGES)), ids=lambda i: LOAN_STAGES[i][0]
)
def test_each_loan_clock_listens_for_every_downstream_stage(index: int) -> None:
    stem, topic = LOAN_STAGES[index]
    doc = _load(LOAN / f"{stem}.json")
    downstream = [t for _, t in LOAN_STAGES[index + 1 :]] + [LOAN_DONE]
    entry = doc["entry"]
    assert entry["topic"] == topic and entry["key"] == "application_id"
    assert entry["reenter"] is True and entry["cooldown_hours"] == 1
    assert entry["on_repeat"] == "refresh_latest" and entry["debounce_minutes"] == 30
    progressed, withdrawn = doc["goals"]
    assert progressed["topics"] == downstream, stem  # exactly, in funnel order
    assert progressed["exit_reason"] == "goal_met" and "key" not in progressed
    assert withdrawn == {"topics": LOAN_OUT, "exit_reason": "withdrawn"}
    assert [(n["type"], n.get("minutes")) for n in doc["nodes"]] == [
        ("wait", 30),
        ("call", None),
    ]
    assert doc["nodes"][1]["template_id"] == "TEMPLATE_ID_PLACEHOLDER"
