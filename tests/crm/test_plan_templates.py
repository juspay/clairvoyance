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

from app.crm.outreach import predicates
from app.crm.outreach.ladder import expand_stages
from app.crm.outreach.plans import Catalogs, validate_definition
from app.crm.outreach.schemas import WorkflowDefinition
from app.crm.record.catalog import code_entries, with_ops
from app.crm.record.contracts import CatalogField

PLANS = Path(__file__).resolve().parents[2] / "docs" / "crm" / "plans"
CART = PLANS / "cart-recovery.json"
CART_FALLBACK = PLANS / "cart-recovery-fallback.json"
CART_TIERED = PLANS / "cart-recovery-tiered.json"
CART_SPLIT = PLANS / "cart-recovery-split.json"
LOAN = PLANS / "loan-dropoff.json"
COD = PLANS / "cod-confirm.json"
LINE = PLANS / "line-nudge.json"
LINE_MOBILE = PLANS / "line-nudge-mobile.json"
LINE_PLAYBOOK = PLANS / "line-nudge-playbook.json"
LINE_CALL_WAIT = PLANS / "line-nudge-call-wait.json"
LINE_CALL_LETTERS = PLANS / "line-nudge-call-letters.json"
# The lending journey on a merchant's OWN events (line-nudge.json): eight
# non-terminal topics the squares listen on, three terminals the goal ends on.
LINE_OPEN = [
    "LINE_INITIATED",
    "LINE_ACCOUNT_AGGREGATOR_REQUIRED",
    "LINE_ACCOUNT_AGGREGATOR_INITIATED",
    "LINE_ACCOUNT_AGGREGATOR_COMPLETED",
    "LINE_OFFERED",
    "LINE_OFFER_SELECTED",
    "LINE_KYC_COMPLETED",
    "LINE_LENDER_ATTEMPT_FAILED",
]
LINE_DONE = ["LINE_ACTIVE", "LINE_HARD_OFFER_REJECTED", "LINE_KYC_REJECTED"]

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
    # A registered row's ops are computed on read (catalog.with_ops), as the
    # server's gather does — so a door's op on a registered field is judged.
    for topic in [*LOAN_STAGES, LOAN_DONE, *LOAN_OUT]:
        catalogs[topic] = {p: with_ops(f) for p, f in _loan_registration().items()}
    for topic in [*LINE_OPEN, *LINE_DONE]:
        catalogs[topic] = {p: with_ops(f) for p, f in _line_registration().items()}
    return catalogs


def _line_registration() -> Dict[str, CatalogField]:
    """What the lending vendor registers per LINE_* topic: the person, the
    key, and the credit-line offers rendered from INSIDE loan_applications —
    only the applications whose facility is a credit line with at least one
    offer, one numbered line per offer, each naming its lender and id."""
    credit_line = [
        {"field": "facility_type", "op": "is", "value": "CREDIT_LINE"},
        {"field": "offers", "op": "exists"},
    ]
    fields = [
        CatalogField(
            path="payload.customer_mobile_number",
            type="phone",
            label="Customer phone",
            identity="phone",
        ),
        CatalogField(
            path="payload.customer_id",
            type="text",
            label="Customer id",
            keyable=True,
            variable=True,
        ),
        CatalogField(
            path="payload.event_name", type="text", label="Event", variable=True
        ),
        CatalogField(
            path="payload.products.sub_category",
            type="list",
            label="Product sub-categories",
            variable=True,
        ),
        CatalogField(
            path="payload.loan_applications",
            type="list",
            label="Credit lines",
            variable=True,
            item_where=credit_line,
            item_format="{lender_name}: {offers.offer_id}",
        ),
        CatalogField(
            path="payload.loan_applications.offers",
            type="list",
            label="Credit-line offers",
            variable=True,
            item_where=credit_line,
            item_numbered=True,
            item_format=(
                "{lender_name} offer {offer_id}: {tenure} months at "
                "{reducing_interest_rate} percent, up to {max_loan_amount} {currency}"
            ),
        ),
    ]
    return {f.path: f for f in fields}


def test_the_expected_documents_exist() -> None:
    assert CART.is_file(), CART
    assert LOAN.is_file(), LOAN
    assert CART_FALLBACK.is_file(), CART_FALLBACK
    assert COD.is_file(), COD
    assert CART_TIERED.is_file(), CART_TIERED
    assert CART_SPLIT.is_file(), CART_SPLIT
    assert LINE.is_file(), LINE
    assert LINE_MOBILE.is_file(), LINE_MOBILE
    assert LINE_PLAYBOOK.is_file(), LINE_PLAYBOOK
    assert LINE_CALL_WAIT.is_file(), LINE_CALL_WAIT
    assert LINE_CALL_LETTERS.is_file(), LINE_CALL_LETTERS
    assert _every_plan() == [
        CART_FALLBACK,
        CART_SPLIT,
        CART_TIERED,
        CART,
        COD,
        LINE_CALL_LETTERS,
        LINE_CALL_WAIT,
        LINE_MOBILE,
        LINE_PLAYBOOK,
        LINE,
        LOAN,
    ]


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


def test_the_tiered_cart_board_calls_only_above_the_threshold() -> None:
    """enh A/01's example: one condition square, `big` -> the rescue call,
    `else` -> the WhatsApp nudge; both arrive at the same closing wait."""
    doc = _load(CART_TIERED)
    decide = next(n for n in doc["nodes"] if n["type"] == "condition")
    assert [r["on"] for r in decide["rules"]] == ["big"]
    assert decide["rules"][0]["if"][0]["field"] == "context.total_price"
    # the whole arrow, destination included: a swapped pair of targets would
    # still carry both labels
    assert sorted(tuple(e) for e in doc["edges"] if e[0] == "decide") == [
        ("decide", "rescue-call", "big"),
        ("decide", "wa-nudge", "else"),
    ]


def test_the_split_cart_board_sends_two_letters_in_a_fixed_share() -> None:
    """enh A/04's example: one split square, 70/30 between two approved
    WhatsApp templates, both arriving at the same closing wait. The shares
    total 100 because a split has no `else` — every run takes an arm."""
    doc = _load(CART_SPLIT)
    node = next(n for n in doc["nodes"] if n["type"] == "split")
    assert [(a["on"], a["percent"]) for a in node["arms"]] == [
        ("control", 70),
        ("variant", 30),
    ]
    assert sum(a["percent"] for a in node["arms"]) == 100
    labels = {(e[0], e[2]) for e in doc["edges"] if len(e) == 3}
    assert {("which-letter", "control"), ("which-letter", "variant")} <= labels
    # the two arms differ in ONE thing — the letter — so the arm counts in
    # the summary are about the letter and nothing else
    letters = {n["id"]: n["template"] for n in doc["nodes"] if n["type"] == "send"}
    assert letters == {"wa-control": "cart_recovery_1", "wa-variant": "cart_recovery_2"}
    assert ["wa-control", "wait-1d"] in doc["edges"]
    assert ["wa-variant", "wait-1d"] in doc["edges"]


def test_the_line_board_calls_only_customers_without_products() -> None:
    """The not_exists example: one condition square reads `context.products
    not_exists` AND `context.offers exists`. No products and an offer ->
    `yes` (quiet, then the call); a letter carrying products -> `else`
    (listen), and a later letter without them re-decides on its own."""
    doc = WorkflowDefinition.model_validate(_load(LINE))
    decide = next(n for n in doc.nodes if n.id == "is-credit-line")
    offers = {"offers": "1. Fibe offer LSP1: 9 months at 22.00 percent"}
    assert predicates.choose(decide.rules, offers, {}, None) == "yes"
    with_products = {**offers, "products": "Apple Mobile Mobile"}
    assert predicates.choose(decide.rules, with_products, {}, None) is None
    assert predicates.choose(decide.rules, {}, {}, None) is None  # no offer either
    assert sorted(
        tuple(e) for e in _load(LINE)["edges"] if e[0] == "is-credit-line"
    ) == [
        ("is-credit-line", "listen", "else"),
        ("is-credit-line", "quiet-30m", "yes"),
    ]


def test_the_line_board_calls_only_inside_the_calling_window() -> None:
    """The window example: quiet-30m's timer fires only 07:00-23:00 IST, so
    the call after it is never queued at night."""
    doc = WorkflowDefinition.model_validate(_load(LINE))
    quiet = next(n for n in doc.nodes if n.id == "quiet-30m")
    assert quiet.window is not None
    assert (quiet.window.opens, quiet.window.closes, quiet.window.timezone) == (
        "07:00",
        "23:00",
        "Asia/Kolkata",
    )
    assert ("quiet-30m", "nudge-call", "timeout") in {
        tuple(e) for e in _load(LINE)["edges"]
    }


def test_every_cart_send_maps_its_blanks() -> None:
    """send_variables posts EXACTLY the map, nothing when it is empty — a
    shipped board with an unmapped send would refuse on every send."""
    for path in (CART, CART_FALLBACK, CART_TIERED, CART_SPLIT):
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
        ("wait", 1440),
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


def test_the_cod_board_declares_no_match_and_does_not_need_one() -> None:
    """The document exists to show what a reply-listening board looks like
    AFTER attribution (docs/crm/reply-run-matching.md): a send, a square
    that listens, and nothing declaring which run a reply belongs to.

    Her tap carries the id of the message it answers; the manifest names the
    run and the square that sent it. Pinning the ABSENCE here because it is
    the whole point, and because a helpful future edit adding a `match` line
    would quietly reintroduce something to get wrong.
    """
    board = _load(COD)
    square = next(n for n in board["nodes"] if n["type"] == "wait" and n.get("topics"))
    assert square["topics"] == ["message.inbound"]
    assert "match" not in square
    assert ["confirm", square["id"]] in [e[:2] for e in board["edges"]]
    labels = {e[2] for e in board["edges"] if len(e) > 2}
    assert {"CONFIRM", "CANCEL", "form_submitted", "timeout"} <= labels


def test_the_mobile_door_asks_the_list_its_one_question_and_nothing_else() -> None:
    """line-nudge-mobile.json gates enrolment with `includes "Mobile"` on a
    list inside `products` (the value written in the plan); an op that
    would compare the list as one value is refused by the catalog law."""
    doc = _load(LINE_MOBILE)
    assert doc["entry"]["where"] == [
        {"field": "payload.products.sub_category", "op": "includes", "value": "Mobile"}
    ]
    doc["entry"]["where"] = [
        {"field": "payload.products.sub_category", "op": "is", "value": "Mobile"}
    ]
    problems = validate_definition(doc, catalogs=_catalogs())
    assert any(
        "payload.products.sub_category" in p
        and "allowed: includes, exists, not_exists" in p
        for p in problems
    ), problems
