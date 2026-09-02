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
from app.crm.outreach.plans import Catalogs, validate_definition
from app.crm.outreach.schemas import WorkflowDefinition
from app.crm.record.catalog import code_entries
from app.crm.record.contracts import CatalogField

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
CART = PLANS / "cart-recovery.json"
CART_FALLBACK = PLANS / "cart-recovery-fallback.json"
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


def _loan_registration() -> Dict[str, CatalogField]:
    """What the loan vendor signs at enrollment (POST /ingest/schemas): the
    one field every loan.* door keys on. The board cannot validate without
    it — which is the point: a keyed door needs its topic in the catalog."""
    field = CatalogField(
        path="payload.application_id",
        type="text",
        label="Application",
        keyable=True,
        ops=["is", "is_not", "in", "exists"],
    )
    return {field.path: field}


def _catalogs() -> Catalogs:
    """The CODE catalog (Shopify's declared fields, by topic) plus the loan
    vendor's registration — so every catalog law runs over the shipped
    boards on every CI run, never `catalogs=None` (which skips them all)."""
    catalogs: Dict[str, Optional[Dict[str, CatalogField]]] = {
        entry.topic: {f.path: f for f in entry.fields} for entry in code_entries()
    }
    for topic in [*LOAN_STAGES, LOAN_DONE, *LOAN_OUT]:
        catalogs[topic] = _loan_registration()
    return catalogs


def test_the_expected_documents_exist() -> None:
    assert CART.is_file(), CART
    assert LOAN.is_file(), LOAN
    assert CART_FALLBACK.is_file(), CART_FALLBACK
    assert _every_plan() == [CART_FALLBACK, CART, LOAN]


@pytest.mark.parametrize("path", _every_plan(), ids=lambda p: p.stem)
def test_every_plan_template_validates(path: Path) -> None:
    assert validate_definition(_load(path), catalogs=_catalogs()) == [], path


def test_the_catalog_laws_actually_run_over_the_boards() -> None:
    """The guard on the guard: with the catalog in hand, a send mapping a
    fact Shopify never declares is refused — so a passing suite means the
    boards' maps and keys were judged, not skipped."""
    doc = _load(CART)
    send = next(n for n in doc["nodes"] if n["type"] == "send")
    send["variables"] = {"1": "loyalty_tier"}
    problems = validate_definition(doc, catalogs=_catalogs())
    assert any("loyalty_tier" in p and "not a declared variable" in p for p in problems)


def test_every_cart_send_maps_its_blanks() -> None:
    """send_variables posts EXACTLY the map, nothing when it is empty — a
    shipped board with an unmapped send would refuse on every send."""
    for path in (CART, CART_FALLBACK):
        for node in _load(path)["nodes"]:
            if node["type"] == "send":
                assert node.get("variables") == {"1": "customer_name"}, (path, node)


def test_cart_recovery_is_the_final_shape_from_the_notes() -> None:
    doc = _load(CART)
    # §16.1: runs are a day long, so a template fix should reach every
    # waiting run — migrate, under the stranding validator (ADR 0023).
    assert doc["on_publish"] == "migrate"
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
    # two applications of one customer are two runs: every listening square
    # hears only the letter about ITS application (phase 18)
    for node in definition.nodes:
        if node.type == "wait_event":
            assert node.match is not None, node.id
            assert (node.match.payload, node.match.run) == ("application_id",) * 2
    # the offer stage waits longer, the others keep the ladder's clock
    assert by_id["at-offer-accepted"].minutes == 120
    assert by_id["at-kyc-completed"].minutes == 30


def test_cart_recovery_fallback_is_the_cart_board_with_the_call_outcome_branch() -> (
    None
):
    """Phase 18 (G2): the same board, and after the rescue call a listening
    square hears THIS run's call.completed (match on enrollment_id) — the
    dispatcher's own no-contact words go to a second WhatsApp, every other
    outcome (the template's own words, or the alarm) keeps the post-call
    listening day so an order within it still counts as recovered."""
    base, doc = _load(CART), _load(CART_FALLBACK)
    for word in ("entry", "goals", "exits", "purpose_key"):
        assert doc[word] == base[word], word
    assert [(n["type"], n.get("minutes")) for n in doc["nodes"]] == [
        ("wait", 30),
        ("send", None),
        ("wait", 30),
        ("call", None),
        ("wait_event", 1440),
        ("send", None),
        ("wait", 1440),
    ]
    assert doc["nodes"][:4] == base["nodes"][:4]
    after_call = doc["nodes"][4]
    assert after_call["topics"] == ["call.completed"] and after_call["key"] == "outcome"
    assert after_call["match"] == {"payload": "enrollment_id", "run": "id"}
    assert doc["nodes"][5]["template"] == "cart_recovery_2"
    labelled = {(e[1], e[2]) for e in doc["edges"] if e[0] == "after-call"}
    assert labelled == {
        ("wa-fallback", "NO_ANSWER"),
        ("wa-fallback", "BUSY"),
        ("wa-fallback", "EARLY_HANGUP"),
        ("wait-1d", "else"),
    }
    assert ["wa-fallback", "wait-1d"] in doc["edges"]
