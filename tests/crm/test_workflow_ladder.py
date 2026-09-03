"""Rollout phase 17: the `stages` ladder. An ordered funnel is written as
one small object and the validator expands it into the wait_event board
of notes §14.1 / §16.2 — every stage listens for every later stage, goes
quiet into its action, then listens once more. The expansion is PURE and
idempotent (node ids derive from the topics), the author's `stages` is
stored beside what it produced, and the walker never learns the word."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.plans as plans
from app.crm.outreach.db import DbTxn
from app.crm.outreach.ladder import LadderProblem, expand_stages
from app.crm.outreach.nodes import run_facts
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import Workflow, WorkflowDefinition
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
THREE = ["loan.profile_created", "loan.kyc_completed", "loan.bank_linked"]


def _ladder(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "key": "application_id",
        "reenter": True,
        "cooldown_hours": 0,
        "on_repeat": "refresh_latest",
        "exits": {"max_age_days": 30},
        "goals": [
            {"topics": ["loan.disbursed"], "exit_reason": "goal_met"},
            {"topics": ["loan.rejected"], "exit_reason": "withdrawn"},
        ],
        "stages": {
            "order": list(THREE),
            "idle_minutes": 30,
            "on_idle": {"type": "call", "template_id": "tpl-dropoff"},
            "after_action_minutes": 1440,
            "restart_on_repeat": True,
        },
    }
    base.update(overrides)
    return base


def _stages(**overrides: Any) -> Dict[str, Any]:
    return {**_ladder()["stages"], **overrides}


def _by_id(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {node["id"]: node for node in doc["nodes"]}


# --- the expansion, exactly ---


def test_three_stages_expand_to_the_exact_board() -> None:
    doc = expand_stages(_ladder())
    # the author's intent is kept beside what it produced
    assert doc["stages"] == _ladder()["stages"]
    assert [n["id"] for n in doc["nodes"]] == [
        "at-profile-created",
        "act-profile-created",
        "after-profile-created",
        "at-kyc-completed",
        "act-kyc-completed",
        "after-kyc-completed",
        "at-bank-linked",
        "act-bank-linked",
    ]
    by_id = _by_id(doc)
    assert by_id["at-profile-created"] == {
        "id": "at-profile-created",
        "type": "wait_event",
        "key": "$topic",
        "topics": ["loan.kyc_completed", "loan.bank_linked"],
        "minutes": 30,
        "stage": "loan.profile_created",
        "match": {"payload": "application_id", "run": "application_id"},
    }
    assert by_id["act-profile-created"] == {
        "id": "act-profile-created",
        "type": "call",
        "template_id": "tpl-dropoff",
        "stage": "loan.profile_created",
    }
    assert by_id["after-profile-created"] == {
        "id": "after-profile-created",
        "type": "wait_event",
        "key": "$topic",
        "topics": ["loan.kyc_completed", "loan.bank_linked"],
        "minutes": 1440,
        "stage": "loan.profile_created",
        "match": {"payload": "application_id", "run": "application_id"},
    }
    assert by_id["at-kyc-completed"]["topics"] == ["loan.bank_linked"]
    assert by_id["after-kyc-completed"]["topics"] == ["loan.bank_linked"]
    assert doc["edges"] == [
        ["at-profile-created", "at-kyc-completed", "loan.kyc_completed"],
        ["at-profile-created", "at-bank-linked", "loan.bank_linked"],
        ["at-profile-created", "act-profile-created", "timeout"],
        ["act-profile-created", "after-profile-created"],
        ["after-profile-created", "at-kyc-completed", "loan.kyc_completed"],
        ["after-profile-created", "at-bank-linked", "loan.bank_linked"],
        ["at-kyc-completed", "at-bank-linked", "loan.bank_linked"],
        ["at-kyc-completed", "act-kyc-completed", "timeout"],
        ["act-kyc-completed", "after-kyc-completed"],
        ["after-kyc-completed", "at-bank-linked", "loan.bank_linked"],
        ["at-bank-linked", "act-bank-linked"],
    ]
    # one door per stage, starting on its listening square; a repeat of
    # the stage's own letter re-arms by the stage's idle time
    assert doc["entry"] == [
        {
            "topic": "loan.profile_created",
            "start": "at-profile-created",
            "debounce_minutes": 30,
            "restart_on_repeat": True,
        },
        {
            "topic": "loan.kyc_completed",
            "start": "at-kyc-completed",
            "debounce_minutes": 30,
            "restart_on_repeat": True,
        },
        {
            "topic": "loan.bank_linked",
            "start": "at-bank-linked",
            "debounce_minutes": 30,
            "restart_on_repeat": True,
        },
    ]
    # the author's other words stay where they were
    for word in ("key", "reenter", "cooldown_hours", "on_repeat", "exits", "goals"):
        assert doc[word] == _ladder()[word]
    # and the board it produced is a valid plan
    assert validate_definition(doc) == []


def test_every_listening_square_lists_every_downstream_stage() -> None:
    """§14.3 objection 5: one missing arrow is one wrong phone call. Every
    at-/after- square listens for ALL later stages and has one labelled
    arrow per later stage to that stage's at- square."""
    order = [f"loan.s{i}" for i in range(1, 6)]
    doc = expand_stages(_ladder(stages=_stages(order=order)))
    definition = WorkflowDefinition.model_validate(doc)
    by_id = {node.id: node for node in definition.nodes}
    outgoing = definition.outgoing()
    for index, topic in enumerate(order[:-1]):
        downstream = order[index + 1 :]
        slug = topic.split(".")[-1]
        for square in (f"at-{slug}", f"after-{slug}"):
            assert by_id[square].topics == downstream, square
            labelled = {on: dst for dst, on in outgoing[square] if on != "timeout"}
            assert labelled == {t: f"at-{t.split('.')[-1]}" for t in downstream}
        assert ("act-" + slug, "timeout") in outgoing[f"at-{slug}"]
        assert not any(on == "timeout" for _, on in outgoing[f"after-{slug}"])
    assert [(d.topic, d.start) for d in definition.entries] == [
        (t, f"at-{t.split('.')[-1]}") for t in order
    ]


def test_the_last_stage_is_a_plain_wait_then_the_action_then_the_end() -> None:
    doc = expand_stages(_ladder())
    by_id = _by_id(doc)
    assert by_id["at-bank-linked"] == {
        "id": "at-bank-linked",
        "type": "wait",
        "minutes": 30,
        "stage": "loan.bank_linked",
    }
    assert "after-bank-linked" not in by_id
    outgoing = WorkflowDefinition.model_validate(doc).outgoing()
    assert outgoing["at-bank-linked"] == [("act-bank-linked", None)]
    assert "act-bank-linked" not in outgoing  # no arrow out = completed


def test_overrides_apply_to_their_stage_only() -> None:
    doc = expand_stages(
        _ladder(
            stages=_stages(
                overrides={
                    "loan.kyc_completed": {
                        "idle_minutes": 120,
                        "after_action_minutes": 60,
                        "on_idle": {"type": "call", "template_id": "tpl-kyc"},
                    }
                }
            )
        )
    )
    by_id = _by_id(doc)
    assert by_id["at-kyc-completed"]["minutes"] == 120
    assert by_id["after-kyc-completed"]["minutes"] == 60
    assert by_id["act-kyc-completed"]["template_id"] == "tpl-kyc"
    assert by_id["at-profile-created"]["minutes"] == 30
    assert by_id["after-profile-created"]["minutes"] == 1440
    assert by_id["act-profile-created"]["template_id"] == "tpl-dropoff"
    # the door re-arms by ITS stage's idle time
    doors = {d["topic"]: d["debounce_minutes"] for d in doc["entry"]}
    assert doors["loan.kyc_completed"] == 120 and doors["loan.profile_created"] == 30
    assert validate_definition(doc) == []


def test_a_send_on_idle_becomes_a_send_square_the_template_laws_see() -> None:
    doc = _ladder(
        purpose_key="utility.loan.dropoff",
        stages=_stages(
            on_idle={"type": "send", "channel": "whatsapp", "template": "loan_nudge"}
        ),
    )
    expanded = expand_stages(doc)
    assert _by_id(expanded)["act-kyc-completed"] == {
        "id": "act-kyc-completed",
        "type": "send",
        "channel": "whatsapp",
        "template": "loan_nudge",
        "stage": "loan.kyc_completed",
    }
    assert validate_definition(doc) == []
    # the templates a run pinned to this board may send (the phase 14 lock)
    assert (
        WorkflowDefinition.model_validate(expanded).send_templates()
        == [("whatsapp", "loan_nudge")] * 3
    )
    # a send ladder still needs the plan's purpose_key — the send law applies
    problems = validate_definition({**doc, "purpose_key": None})
    assert any("act-profile-created" in p and "purpose_key" in p for p in problems)


def test_expanding_twice_is_the_same_document() -> None:
    once = expand_stages(_ladder())
    assert expand_stages(once) == once
    assert validate_definition(once) == []


def test_the_expanded_square_hands_its_stage_to_the_call() -> None:
    """Phase 16's current_stage rides from the square the ladder labelled."""
    definition = WorkflowDefinition.model_validate(expand_stages(_ladder()))
    square = next(n for n in definition.nodes if n.id == "at-kyc-completed")
    facts = run_facts({"application_id": "APP-1"}, square)
    assert facts["current_stage"] == "loan.kyc_completed"
    assert facts["current_node"] == "at-kyc-completed"


# --- the laws ---


def test_a_ladder_does_not_carry_nodes_edges_or_entry_of_its_own() -> None:
    hand_drawn = {
        "nodes": [{"id": "w", "type": "wait", "minutes": 1}],
        "edges": [],
        "entry": [{"topic": "loan.profile_created", "start": "w"}],
    }
    for word, value in hand_drawn.items():
        problems = validate_definition(_ladder(**{word: value}))
        assert any("stages" in p and word in p for p in problems), word
        with pytest.raises(LadderProblem):
            expand_stages(_ladder(**{word: value}))
    # its OWN expansion beside it is the stored form, and reads as itself
    assert validate_definition(expand_stages(_ladder())) == []


def test_a_ladder_owns_the_debounce_and_restart_words() -> None:
    """Each door debounces by its stage's idle time and restarts per the
    ladder — a top-level spelling would silently lose to that."""
    for word, value in (("debounce_minutes", 10), ("restart_on_repeat", True)):
        problems = validate_definition(_ladder(**{word: value}))
        assert any("stages" in p and word in p for p in problems), word
    # the other shared words still reach every door
    doors = WorkflowDefinition.model_validate(expand_stages(_ladder())).entries
    assert all(
        d.key == "application_id"
        and d.reenter
        and d.cooldown_hours == 0
        and d.on_repeat == "refresh_latest"
        for d in doors
    )


def test_the_ladders_own_shape_laws() -> None:
    def problems_of(stages: Dict[str, Any]) -> List[str]:
        return validate_definition(_ladder(stages=stages))

    # fewer than two stages is not a ladder
    assert any("2" in p for p in problems_of(_stages(order=["loan.only"])))
    # a stage named twice
    twice = _stages(order=["loan.a", "loan.b", "loan.a"])
    assert any("loan.a" in p and "twice" in p for p in problems_of(twice))
    # an override for a stage that is not in the order (a typo, not a rule)
    stray = _stages(overrides={"loan.nope": {"idle_minutes": 5}})
    assert any("loan.nope" in p and "overrides" in p for p in problems_of(stray))
    # two topics that slug to the same square
    clash = _stages(order=["loan.kyc", "card.kyc"])
    assert any("loan.kyc" in p and "card.kyc" in p for p in problems_of(clash))
    # the clocks must be positive
    assert any("idle_minutes" in p for p in problems_of(_stages(idle_minutes=0)))
    assert any(
        "after_action_minutes" in p
        for p in problems_of(_stages(after_action_minutes=0))
    )
    # the action is a call or a send, nothing else
    assert any("on_idle" in p for p in problems_of(_stages(on_idle={"type": "wait"})))
    # a ladder with no stages at all is refused, not silently a board
    assert problems_of({}) != []


# --- where the expansion runs: create, draft, publish ---


def _workflow(
    status: str, definition: Optional[Dict[str, Any]], draft: Any
) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="loan-dropoff",
        status=status,
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=definition,
        draft=draft,
    )


@pytest.mark.asyncio
async def test_create_and_draft_store_the_ladder_and_its_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: List[Dict[str, Any]] = []

    async def insert_workflow(
        merchant_id: str, name: str, draft: Dict[str, Any], created_by: Optional[str]
    ) -> Workflow:
        stored.append(draft)
        return _workflow("draft", None, draft)

    async def update_draft(
        merchant_id: str, workflow_id: str, draft: Dict[str, Any]
    ) -> Workflow:
        stored.append(draft)
        return _workflow("draft", None, draft)

    monkeypatch.setattr(plans.workflow_accessor, "insert_workflow", insert_workflow)
    monkeypatch.setattr(plans.workflow_accessor, "update_draft", update_draft)
    await plans.create_workflow("m1", "loan-dropoff", _ladder(), "ops@x")
    await plans.update_draft("m1", "wf-1", _ladder())
    assert stored == [expand_stages(_ladder())] * 2
    for draft in stored:
        assert draft["stages"] == _ladder()["stages"]
        assert {"nodes", "edges", "entry"} <= set(draft)
    # a hand-drawn board is stored as written — no ladder, no expansion
    board = expand_stages(_ladder())
    plain = {k: v for k, v in board.items() if k != "stages"}
    await plans.create_workflow("m1", "board", plain, "ops@x")
    assert stored[-1] == plain and "stages" not in stored[-1]


class _PublishAccessor:
    def __init__(self, draft: Dict[str, Any]) -> None:
        self.draft = draft
        self.versions: List[Tuple[int, Dict[str, Any]]] = []

    async def workflow_for_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow("draft", None, self.draft)

    async def occupied_nodes(self, conn: Any, m: str, w: str) -> List[str]:
        return []

    async def lock_templates_shared(
        self, conn: Any, m: str, templates: List[Tuple[str, str]]
    ) -> None:
        return None

    async def apply_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow("live", self.draft, None)

    async def insert_version(
        self, conn: Any, m: str, w: str, version: int, doc: Dict[str, Any], *rest: Any
    ) -> None:
        self.versions.append((version, doc))

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        return 0


@pytest.mark.asyncio
async def test_publish_takes_the_stored_ladder_and_refuses_a_drifted_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft on disk is the ladder plus its board. Publish re-expands
    and must find the same board — a board that no longer matches its
    ladder (the expander changed since the draft was saved) is refused,
    never published half-and-half."""
    stored = expand_stages(_ladder())
    accessor = _PublishAccessor(stored)
    patch_accessors(monkeypatch, plans, accessor)
    published = await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)
    assert published.definition == stored
    assert accessor.versions == [(1, stored)]

    drifted = {
        **stored,
        "nodes": [
            {**n, "minutes": 5} if n["id"] == "at-kyc-completed" else n
            for n in stored["nodes"]
        ],
    }
    patch_accessors(monkeypatch, plans, _PublishAccessor(drifted))
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)
    assert any("stages" in p and "nodes" in p for p in refused.value.problems)

    # a ladder saved WITHOUT its board (never by create/draft — a direct
    # write) is refused as well: apply_publish copies the draft verbatim,
    # and a live document without squares would park every run
    patch_accessors(monkeypatch, plans, _PublishAccessor(_ladder()))
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)
    assert any("without its board" in p for p in refused.value.problems)


def test_a_keyed_ladder_listens_only_for_letters_about_its_own_key() -> None:
    """Phase 18: the consumer wakes every open run of the CUSTOMER whose
    square listens for a topic — with two applications (two runs keyed
    by application_id) a KYC letter for one would move both. A keyed
    ladder sets `match` on every listening square from the document's
    key: the letter's application_id must equal the run's. An unkeyed
    ladder listens for every letter of the customer, as before; the last
    stage's plain wait listens for nothing and carries no match."""
    keyed = expand_stages(_ladder())
    for node in keyed["nodes"]:
        if node["type"] == "wait_event":
            assert node["match"] == {
                "payload": "application_id",
                "run": "application_id",
            }, node["id"]
        else:
            assert "match" not in node, node["id"]
    unkeyed = expand_stages({k: v for k, v in _ladder().items() if k != "key"})
    assert all("match" not in node for node in unkeyed["nodes"])
    assert validate_definition(keyed) == [] and validate_definition(unkeyed) == []
