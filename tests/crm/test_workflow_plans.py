"""W1 publish-validator laws: the exact edit classes canon T19 says the
validator must block, each as a red test."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.plans as plans
from app.crm.connectivity.schemas.template import TemplateVerdict
from app.crm.outreach.db import DbTxn
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    Workflow,
    WorkflowDefinition,
)
from app.crm.record.catalog import code_entries
from app.crm.record.schemas import CatalogField
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def _definition(**overrides):
    base = {
        "entry": {
            "topic": "checkout.initiated",
            "reenter": True,
            "cooldown_hours": 0,
        },
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {"id": "rescue-call", "type": "call", "template_id": "tpl-1"},
        ],
        "edges": [["wait-30m", "rescue-call"]],
        "goal": {"topics": ["order.placed"]},
    }
    base.update(overrides)
    return base


def test_valid_definition_passes() -> None:
    assert validate_definition(_definition()) == []


def test_duplicate_node_ids_fail() -> None:
    problems = validate_definition(
        _definition(
            nodes=[
                {"id": "a", "type": "wait", "minutes": 5},
                {"id": "a", "type": "call", "template_id": "t"},
            ],
            edges=[],
        )
    )
    assert any("duplicate node id" in p for p in problems)


def test_edge_to_unknown_node_fails() -> None:
    problems = validate_definition(_definition(edges=[["wait-30m", "ghost"]]))
    assert any("unknown node: ghost" in p for p in problems)


def test_two_plain_edges_out_of_one_node_fail() -> None:
    problems = validate_definition(
        _definition(
            nodes=[
                {"id": "a", "type": "wait", "minutes": 1},
                {"id": "b", "type": "wait", "minutes": 1},
                {"id": "c", "type": "wait", "minutes": 1},
            ],
            edges=[["a", "b"], ["a", "c"]],
        )
    )
    assert any("2 outgoing edges" in p for p in problems)


def test_wait_without_minutes_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "w", "type": "wait"}], edges=[])
    )
    assert any("needs minutes" in p for p in problems)


def test_call_without_template_id_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "c", "type": "call"}], edges=[])
    )
    assert any("needs a template_id" in p for p in problems)


def test_send_without_template_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "s", "type": "send"}], edges=[])
    )
    assert any("needs a template" in p for p in problems)


def test_unknown_node_type_fails_shape() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "x", "type": "teleport"}], edges=[])
    )
    assert any("shape invalid" in p for p in problems)


def test_exit_ceiling_must_be_positive() -> None:
    for bad in (0, -1):
        problems = validate_definition(_definition(exits={"max_age_days": bad}))
        assert problems and "shape invalid" in problems[0]
    assert validate_definition(_definition(exits={"max_age_days": 0.5})) == []


def test_cooldown_cannot_be_negative() -> None:
    entry = {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": -1}
    problems = validate_definition(_definition(entry=entry))
    assert problems and "shape invalid" in problems[0]


def test_occupied_node_deletion_fails_in_migrate_mode() -> None:
    """The stranding law, now a migrate-mode precondition (ADR 0023): a
    document that removes a square waiting tokens stand on must not be
    migrated onto them."""
    problems = validate_definition(
        _definition(on_publish="migrate"), occupied_nodes=["old-node"]
    )
    assert any("waiting runs standing on it" in p for p in problems)


def test_occupied_node_deletion_is_fine_under_pin() -> None:
    """pin (the default): runs in flight keep their version, so a new
    document cannot strand anyone — the refusal does not apply."""
    assert validate_definition(_definition(), occupied_nodes=["old-node"]) == []
    assert (
        validate_definition(_definition(on_publish="pin"), occupied_nodes=["old-node"])
        == []
    )


def test_occupied_node_kept_passes() -> None:
    assert validate_definition(_definition(), occupied_nodes=["wait-30m"]) == []


def test_publish_refuses_an_entry_change_while_runs_are_open_in_migrate_mode() -> None:
    draft = {
        "entry": {"topic": "cart.abandoned"},
        "nodes": [{"id": "w", "type": "wait", "minutes": 30}],
        "edges": [],
        "goal": {"topics": ["order.placed"]},
        "on_publish": "migrate",
    }
    live_entry = {"topic": "checkout.initiated"}
    assert validate_definition(draft, occupied_nodes=["w"], live_entry=live_entry)
    assert not validate_definition(draft, occupied_nodes=[], live_entry=live_entry)
    assert not validate_definition(draft, occupied_nodes=["w"], live_entry=None)
    pinned = {**draft, "on_publish": "pin"}
    assert not validate_definition(pinned, occupied_nodes=["w"], live_entry=live_entry)


_COD = {
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


def test_wait_event_with_labelled_edges_passes() -> None:
    assert validate_definition(_COD) == []


def test_wait_event_needs_topics_key_and_minutes() -> None:
    bad = {**_COD, "nodes": [{"id": "ask", "type": "wait_event"}, *_COD["nodes"][1:]]}
    problems = validate_definition(bad)
    assert any("needs minutes" in p for p in problems)
    assert any("needs topics" in p for p in problems)
    assert any("needs a payload key" in p for p in problems)


def test_edges_out_of_wait_event_must_be_labelled_and_distinct() -> None:
    unlabelled = {**_COD, "edges": [["ask", "confirm"]]}
    assert any("needs an on" in p for p in validate_definition(unlabelled))
    twice = {**_COD, "edges": [["ask", "confirm", "YES"], ["ask", "call", "YES"]]}
    assert any("same on" in p for p in validate_definition(twice))


def test_only_wait_event_may_label_edges() -> None:
    bad = {**_COD, "edges": [["ask", "confirm", "YES"], ["confirm", "call", "YES"]]}
    assert any("only a wait_event" in p for p in validate_definition(bad))


def test_send_node_needs_channel_and_a_plan_purpose() -> None:
    bare = _definition(
        nodes=[{"id": "ask", "type": "send", "template": "cod_confirm"}], edges=[]
    )
    problems = validate_definition(bare)
    assert any("needs a channel" in p for p in problems)
    assert any("purpose_key" in p for p in problems)
    full = _definition(
        nodes=[
            {
                "id": "ask",
                "type": "send",
                "template": "cod_confirm",
                "channel": "whatsapp",
            }
        ],
        edges=[],
        purpose_key="utility.order.cod_confirm",
    )
    assert validate_definition(full) == []


def test_entry_key_is_document_vocabulary() -> None:
    keyed = {
        "topic": "checkout.initiated",
        "reenter": True,
        "cooldown_hours": 0,
        "key": "order_id",
    }
    assert validate_definition(_definition(entry=keyed)) == []
    empty = {**keyed, "key": ""}
    problems = validate_definition(_definition(entry=empty))
    assert problems and "shape invalid" in problems[0]


def test_changing_entry_key_mid_flight_is_blocked_in_migrate_mode() -> None:
    live_entry = {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": 0}
    keyed = {**live_entry, "key": "order_id"}
    problems = validate_definition(
        _definition(entry=keyed, on_publish="migrate"),
        occupied_nodes=["wait-30m"],
        live_entry=live_entry,
    )
    assert any("entry rule changed" in p for p in problems)


# --- rollout phase 01: B3 (entry compared by meaning) and B4 (draft -> live) ---

_TERSE_DRAFT = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [{"id": "w", "type": "wait", "minutes": 30}],
    "edges": [],
    "goal": {"topics": ["order.placed"]},
}
_SPELLED_OUT_ENTRY = {
    "topic": "checkout.initiated",
    "where": {},
    "reenter": False,
    "cooldown_hours": 24.0,
    "key": None,
}


def test_publish_compares_the_live_entry_by_meaning_not_spelling() -> None:
    """B3: the live entry is whatever dict the last publish stored. A draft
    that omits the defaults compared unequal to a live entry that spelled
    them out (or the reverse) — a spurious "entry rule changed" refusal on
    a re-publish that changed nothing about admission."""
    migrating = {**_TERSE_DRAFT, "on_publish": "migrate"}
    assert (
        validate_definition(
            migrating, occupied_nodes=["w"], live_entry=_SPELLED_OUT_ENTRY
        )
        == []
    )
    explicit = {**migrating, "entry": _SPELLED_OUT_ENTRY}
    assert (
        validate_definition(
            explicit, occupied_nodes=["w"], live_entry={"topic": "checkout.initiated"}
        )
        == []
    )


def test_a_live_entry_that_no_longer_parses_is_compared_raw() -> None:
    """A legacy row whose entry fails today's shape cannot be normalised;
    the raw dicts are compared as before, so a real change is still caught."""
    problems = validate_definition(
        {**_TERSE_DRAFT, "on_publish": "migrate"},
        occupied_nodes=["w"],
        live_entry={"topic": ""},
    )
    assert any("entry rule changed" in p for p in problems)


def _workflow(status: str, definition) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="plan",
        status=status,
        version=0 if definition is None else 1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=definition,
        draft=_TERSE_DRAFT,
    )


@pytest.mark.asyncio
async def test_going_live_on_a_never_published_draft_is_refused_before_the_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: migration 057's CHECK (status = 'draft' OR definition IS NOT
    NULL) used to fire as an asyncpg error -> HTTP 500. The pre-read
    refuses in logic (a 422), and the UPDATE is never issued — no driver
    exception is raised, let alone caught outside db/."""

    async def get_workflow(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow("draft", None)

    async def set_workflow_status(*args: object) -> None:
        raise AssertionError("must not reach the UPDATE")

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(
        plans.workflow_accessor, "set_workflow_status", set_workflow_status
    )
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await plans.set_status("m1", "wf-1", "live")
    assert "publish" in refused.value.problems[0]


@pytest.mark.asyncio
async def test_pausing_or_archiving_a_never_published_draft_is_refused_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """057 admits a NULL definition only while status = 'draft', so paused
    and archived hit the same CHECK as live — one pre-read covers all."""

    async def get_workflow(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow("draft", None)

    async def set_workflow_status(*args: object) -> None:
        raise AssertionError("must not reach the UPDATE")

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(
        plans.workflow_accessor, "set_workflow_status", set_workflow_status
    )
    for wanted in ("paused", "archived"):
        with pytest.raises(plans.WorkflowValidationError) as refused:
            await plans.set_status("m1", "wf-1", wanted)
        assert "publish a draft" in refused.value.problems[0], wanted


@pytest.mark.asyncio
async def test_a_published_plan_still_pauses_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = _workflow("live", _TERSE_DRAFT)
    writes: list = []

    async def get_workflow(merchant_id: str, workflow_id: str) -> Workflow:
        return published

    async def set_workflow_status(
        merchant_id: str, workflow_id: str, status: str
    ) -> Workflow:
        writes.append(status)
        return published

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(
        plans.workflow_accessor, "set_workflow_status", set_workflow_status
    )
    assert await plans.set_status("m1", "wf-1", "paused") is published
    assert await plans.set_status("m1", "wf-1", "live") is published
    assert writes == ["paused", "live"]


@pytest.mark.asyncio
async def test_unknown_or_archived_plans_answer_none_for_the_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def set_workflow_status(*args: object) -> None:
        raise AssertionError("must not reach the UPDATE")

    monkeypatch.setattr(
        plans.workflow_accessor, "set_workflow_status", set_workflow_status
    )

    async def missing(merchant_id: str, workflow_id: str) -> None:
        return None

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", missing)
    assert await plans.set_status("m1", "wf-1", "live") is None

    async def archived(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow("archived", None)

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", archived)
    assert await plans.set_status("m1", "wf-1", "live") is None


# --- rollout phase 08 (G12): publish asks the template registry about every send node ---

_SEND_PLAN: Dict[str, Any] = {
    "entry": {"topic": "checkouts/update", "reenter": True, "cooldown_hours": 0},
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {
            "id": "wa-nudge",
            "type": "send",
            "channel": "whatsapp",
            "template": "cart_recovery_1",
        },
    ],
    "edges": [["wait-30m", "wa-nudge"]],
    "goal": {"topics": ["orders/create"]},
    "purpose_key": "marketing.cart.recovery",
}


class _PublishAccessor:
    """The accessor slice _publish_in_txn touches: a live plan with a draft
    waiting, no occupied squares, and apply_publish recording whether the
    copy happened."""

    def __init__(self, draft: Dict[str, Any]) -> None:
        self.draft = draft
        self.published = False
        self.locked: List[List[Tuple[str, str]]] = []
        self.order: List[str] = []

    async def workflow_for_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow("live", _TERSE_DRAFT).model_copy(update={"draft": self.draft})

    async def occupied_nodes(self, conn: Any, m: str, w: str) -> List[str]:
        return []

    async def lock_templates_shared(
        self, conn: Any, m: str, templates: List[Tuple[str, str]]
    ) -> None:
        self.locked.append(list(templates))
        self.order.append("lock")

    async def apply_publish(self, conn: Any, m: str, w: str) -> Workflow:
        self.published = True
        self.order.append("publish")
        return _workflow("live", self.draft)

    async def insert_version(self, conn: Any, *args: Any) -> None:
        return None  # phase 11's row; pinned by test_workflow_versions.py

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        return 0


def _verdict(status: Optional[str]) -> TemplateVerdict:
    """The registry's verdict for a test's shorthand: None = never
    registered, "approved" = publishable, any other word = that status."""
    if status is None:
        return TemplateVerdict(
            publishable=False, reason="is not registered on whatsapp for this merchant"
        )
    if status == "approved":
        return TemplateVerdict(publishable=True)
    return TemplateVerdict(publishable=False, reason=f"is '{status}', not approved")


def _registry(
    monkeypatch: pytest.MonkeyPatch, status: Optional[str], registers: bool = True
) -> List[Tuple[str, str, str]]:
    asked: List[Tuple[str, str, str]] = []

    async def template_status(
        merchant_id: str, channel: str, name: str
    ) -> TemplateVerdict:
        asked.append((merchant_id, channel, name))
        return _verdict(status)

    monkeypatch.setattr(plans, "template_status", template_status)
    monkeypatch.setattr(plans, "registers_templates_for", lambda channel: registers)
    return asked


async def _publish(accessor: _PublishAccessor) -> Workflow:
    return await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)


@pytest.mark.asyncio
async def test_publish_refuses_a_send_node_whose_template_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G12: today the first sign of a wrong template name is a blocked
    send hours later. The registry can answer at publish."""
    accessor = _PublishAccessor(_SEND_PLAN)
    patch_accessors(monkeypatch, plans, accessor)
    asked = _registry(monkeypatch, status=None)
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await _publish(accessor)
    assert refused.value.problems == [
        "send node wa-nudge: template 'cart_recovery_1' is not registered on "
        "whatsapp for this merchant"
    ]
    assert asked == [("m1", "whatsapp", "cart_recovery_1")]
    assert accessor.published is False


@pytest.mark.asyncio
async def test_publish_refuses_a_send_node_whose_template_is_not_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_SEND_PLAN)
    patch_accessors(monkeypatch, plans, accessor)
    _registry(monkeypatch, status="pending")
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await _publish(accessor)
    assert refused.value.problems == [
        "send node wa-nudge: template 'cart_recovery_1' is 'pending', not approved"
    ]
    assert accessor.published is False


@pytest.mark.asyncio
async def test_publish_proceeds_when_the_template_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_SEND_PLAN)
    patch_accessors(monkeypatch, plans, accessor)
    _registry(monkeypatch, status="approved")
    published = await _publish(accessor)
    assert accessor.published is True and published.status == "live"


@pytest.mark.asyncio
async def test_a_plan_without_send_nodes_never_asks_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_TERSE_DRAFT)
    patch_accessors(monkeypatch, plans, accessor)
    asked = _registry(monkeypatch, status=None)
    await _publish(accessor)
    assert asked == [] and accessor.published is True


@pytest.mark.asyncio
async def test_a_channel_that_does_not_register_templates_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_SEND_PLAN)
    patch_accessors(monkeypatch, plans, accessor)
    asked = _registry(monkeypatch, status=None, registers=False)
    await _publish(accessor)
    assert asked == [] and accessor.published is True


@pytest.mark.asyncio
async def test_publish_holds_the_templates_it_sends_before_asking_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 14: the template lock (shared/locks.py) is taken SHARED for
    every template the draft sends BEFORE the approval check, so a
    retirement cannot commit between "approved" and the version row."""
    accessor = _PublishAccessor(_SEND_PLAN)
    patch_accessors(monkeypatch, plans, accessor)

    async def template_status(
        merchant_id: str, channel: str, name: str
    ) -> TemplateVerdict:
        accessor.order.append("ask")
        return TemplateVerdict(publishable=True)

    monkeypatch.setattr(plans, "template_status", template_status)
    monkeypatch.setattr(plans, "registers_templates_for", lambda channel: True)
    await _publish(accessor)
    expected = WorkflowDefinition.model_validate(_SEND_PLAN).send_templates()
    assert expected and accessor.locked == [expected]
    assert accessor.order == ["lock", "ask", "publish"]


# --- rollout phase 15: entry as a list of doors, and $topic on wait_event ---

_LADDER: Dict[str, Any] = {
    "entry": [
        {"topic": "loan.profile_created", "start": "at-profile"},
        {"topic": "loan.kyc_completed", "start": "at-kyc"},
    ],
    "reenter": True,
    "cooldown_hours": 0,
    "key": "application_id",
    "nodes": [
        {
            "id": "at-profile",
            "type": "wait_event",
            "key": "$topic",
            "minutes": 30,
            "topics": ["loan.kyc_completed"],
        },
        {"id": "call-profile", "type": "call", "template_id": "tpl-p"},
        {"id": "at-kyc", "type": "wait", "minutes": 30},
        {"id": "call-kyc", "type": "call", "template_id": "tpl-k"},
    ],
    "edges": [
        ["at-profile", "at-kyc", "loan.kyc_completed"],
        ["at-profile", "call-profile", "timeout"],
        ["at-kyc", "call-kyc"],
    ],
    "goal": {"topics": ["loan.disbursed"]},
}


def test_entry_as_a_list_of_doors_validates_and_a_single_entry_still_does() -> None:
    assert validate_definition(_LADDER) == []
    doors = WorkflowDefinition.model_validate(_LADDER).entries
    assert [(d.topic, d.start) for d in doors] == [
        ("loan.profile_created", "at-profile"),
        ("loan.kyc_completed", "at-kyc"),
    ]
    # the shared words at the top level reach every door
    assert all(
        d.reenter and d.cooldown_hours == 0 and d.key == "application_id" for d in doors
    )
    # a door may override them
    overridden = {
        **_LADDER,
        "entry": [{**_LADDER["entry"][0], "cooldown_hours": 2}, _LADDER["entry"][1]],
    }
    d0, d1 = WorkflowDefinition.model_validate(overridden).entries
    assert (d0.cooldown_hours, d1.cooldown_hours) == (2, 0)
    # a single-entry plan is one door starting on nodes[0]
    (door,) = WorkflowDefinition.model_validate(_definition()).entries
    assert (door.topic, door.start) == ("checkout.initiated", "wait-30m")
    assert validate_definition(_definition()) == []


def test_a_plan_needs_at_least_one_door() -> None:
    """An empty door list would parse, publish, and never start a run —
    and enrol() has no first door to fall back on."""
    problems = validate_definition({**_LADDER, "entry": []})
    assert any("at least one door" in p for p in problems)


def test_a_door_must_start_on_a_real_square_and_topics_must_be_unique() -> None:
    bad_start = {
        **_LADDER,
        "entry": [{"topic": "loan.profile_created", "start": "nowhere"}],
    }
    problems = validate_definition(bad_start)
    assert any("nowhere" in p and "start" in p for p in problems)
    twice = {
        **_LADDER,
        "entry": [
            {"topic": "loan.profile_created", "start": "at-profile"},
            {"topic": "loan.profile_created", "start": "at-kyc"},
        ],
    }
    problems = validate_definition(twice)
    assert any("loan.profile_created" in p and "twice" in p for p in problems)


def test_topic_is_the_only_dollar_word_a_wait_event_may_branch_on() -> None:
    assert validate_definition(_LADDER) == []
    other = {
        **_LADDER,
        "nodes": [{**_LADDER["nodes"][0], "key": "$payload"}] + _LADDER["nodes"][1:],
    }
    problems = validate_definition(other)
    assert any("$payload" in p and "$topic" in p for p in problems)


def test_debounce_needs_a_wait_at_each_doors_own_start() -> None:
    bouncing = {
        **_LADDER,
        "entry": [
            {
                "topic": "loan.profile_created",
                "start": "call-profile",
                "debounce_minutes": 10,
            },
            {"topic": "loan.kyc_completed", "start": "at-kyc", "debounce_minutes": 10},
        ],
    }
    problems = validate_definition(bouncing)
    assert any("loan.profile_created" in p and "alarm" in p for p in problems)
    assert not any("loan.kyc_completed" in p for p in problems)


def test_the_migrate_guard_compares_doors_as_a_list() -> None:
    live = _LADDER["entry"]
    same = {**_LADDER, "on_publish": "migrate"}
    assert validate_definition(same, occupied_nodes=["at-kyc"], live_entry=live) == []
    moved = {
        **same,
        "entry": [
            _LADDER["entry"][0],
            {"topic": "loan.kyc_completed", "start": "call-kyc"},
        ],
    }
    problems = validate_definition(moved, occupied_nodes=["at-kyc"], live_entry=live)
    assert any("entry rule changed" in p for p in problems)


# --- rollout phase 16: a square may carry a stage label; a door the restart word ---


def test_a_square_may_carry_a_stage_label_and_a_door_the_restart_word() -> None:
    doc = {
        **_LADDER,
        "restart_on_repeat": True,
        "debounce_minutes": 10,
        "nodes": [{**_LADDER["nodes"][0], "stage": "profile"}] + _LADDER["nodes"][1:],
    }
    assert validate_definition(doc) == []
    definition = WorkflowDefinition.model_validate(doc)
    assert definition.nodes[0].stage == "profile" and definition.nodes[1].stage is None
    assert all(
        d.restart_on_repeat and d.debounce_minutes == 10 for d in definition.entries
    )


# --- rollout phase 18: a listening square may say WHOSE letter it hears, and
# may carry one catch-all arrow ---


def _after_call(**words: Any) -> Dict[str, Any]:
    return {
        "id": "after-call",
        "type": "wait_event",
        "topics": ["call.completed"],
        "key": "outcome",
        "minutes": 1440,
        **words,
    }


_CALL_THEN_LISTEN: List[Dict[str, Any]] = [
    {"id": "call", "type": "call", "template_id": "t"},
    _after_call(match={"payload": "enrollment_id", "run": "id"}),
    {"id": "again", "type": "wait", "minutes": 5},
    {"id": "done", "type": "wait", "minutes": 5},
]


def test_match_names_a_payload_field_and_a_run_field() -> None:
    doc = _definition(
        nodes=_CALL_THEN_LISTEN,
        edges=[["call", "after-call"], ["after-call", "again", "NO_ANSWER"]],
    )
    assert validate_definition(doc) == []
    square = WorkflowDefinition.model_validate(doc).nodes[1]
    assert square.match is not None
    assert (square.match.payload, square.match.run) == ("enrollment_id", "id")
    # both halves are required and non-empty
    for bad in ({"payload": "enrollment_id"}, {"payload": "", "run": "id"}):
        problems = validate_definition(
            _definition(nodes=[_after_call(match=bad)], edges=[])
        )
        assert any("shape invalid" in p for p in problems), bad


def test_only_a_listening_square_may_say_whose_letter_it_hears() -> None:
    problems = validate_definition(
        _definition(
            nodes=[
                {
                    "id": "w",
                    "type": "wait",
                    "minutes": 5,
                    "match": {"payload": "enrollment_id", "run": "id"},
                }
            ],
            edges=[],
        )
    )
    assert any("w" in p and "match" in p and "wait_event" in p for p in problems)


def test_else_is_one_catch_all_arrow_out_of_a_listening_square() -> None:
    edges = [
        ["call", "after-call"],
        ["after-call", "again", "NO_ANSWER"],
        ["after-call", "done", "else"],
    ]
    assert validate_definition(_definition(nodes=_CALL_THEN_LISTEN, edges=edges)) == []
    twice = edges + [["after-call", "again", "else"]]
    problems = validate_definition(_definition(nodes=_CALL_THEN_LISTEN, edges=twice))
    assert any("after-call" in p and "same on" in p for p in problems)
    # a plain square still has one unlabelled arrow, else included
    plain = [["call", "after-call"], ["again", "done", "else"]]
    problems = validate_definition(_definition(nodes=_CALL_THEN_LISTEN, edges=plain))
    assert any("only a wait_event" in p and "again" in p for p in problems)


def _orders_create_catalog() -> Dict[str, CatalogField]:
    entry = next(e for e in code_entries() if e.topic == "orders/create")
    return {f.path: f for f in entry.fields}


def test_each_door_is_checked_against_its_own_topics_catalog() -> None:
    """Phase 15 x the catalog: the laws run once per door. A declared
    field on the orders/create door passes; a where on a door whose topic
    no layer knows is refused; a door-list still refuses the legacy map."""
    doors = {
        **_LADDER,
        "entry": [
            {
                "topic": "orders/create",
                "start": "at-profile",
                "where": [{"field": "payload.gateway", "op": "is", "value": "COD"}],
            },
            {
                "topic": "loan.kyc_completed",
                "start": "at-kyc",
                "where": [{"field": "payload.stage", "op": "is", "value": "kyc"}],
            },
        ],
    }
    del doors["key"]  # application_id is not a declared field on orders/create
    catalogs: plans.Catalogs = {
        "orders/create": _orders_create_catalog(),
        "loan.kyc_completed": None,
    }
    problems = validate_definition(doors, catalogs=catalogs)
    assert problems == [
        "topic 'loan.kyc_completed' is not in the catalog — register its schema "
        "(or declare it in code) before filtering, keying or templating on it"
    ]
    legacy = {
        **doors,
        "entry": [doors["entry"][0], {**doors["entry"][1], "where": {"stage": "kyc"}}],
    }
    assert any("equality map is retired" in p for p in validate_definition(legacy))


def _send(**words) -> Dict[str, Any]:
    node = {"id": "confirm", "type": "send", "channel": "whatsapp", "template": "t"}
    node.update(words)
    return {
        **_COD,
        "nodes": [node],
        "edges": [],
        "purpose_key": "utility.order.confirm",
    }


def test_send_variables_map_shape_laws() -> None:
    assert validate_definition(_send()) == []  # no blanks: zero parameters
    assert validate_definition(_send(variables={"1": "customer_name"})) == []
    problems = validate_definition(_send(variables={"1": "name", "order_no": "name"}))
    assert any("mix positional" in p for p in problems)
    problems = validate_definition(_send(variables={"": "name"}))
    assert any("template blank on the left" in p for p in problems)


def test_send_variables_must_be_declared_variable_fields_on_the_entry_topic() -> None:
    """event-catalog.md: template variables ONLY from declared fields —
    the derived customer_name and Shopify's own order name pass; a typo,
    a bool field, and an identity (phone) do not."""
    catalogs: plans.Catalogs = {"orders/create": _orders_create_catalog()}
    ok = _send(variables={"customer_name": "customer_name", "order_no": "name"})
    assert validate_definition(ok, catalogs=catalogs) == []
    for fact in ("customer_nmae", "confirmed", "phone"):
        problems = validate_definition(_send(variables={"1": fact}), catalogs=catalogs)
        assert any(
            f"<- {fact!r} is not a declared variable field" in p for p in problems
        ), fact
    # an unknown topic may trigger a plan, but its facts cannot fill a template
    unknown = {**_send(variables={"1": "name"}), "entry": {"topic": "ride.done"}}
    problems = validate_definition(unknown, catalogs={"ride.done": None})
    assert any("before filtering, keying or templating" in p for p in problems)


def test_send_variables_may_name_a_stage_letters_fact_and_the_square() -> None:
    """Phase 16 x the map: a wait_event square's letter is reachable as
    facts_<square>_<key> for the variable fields its topics declare, and
    the walker's own current_node / current_stage are always allowed."""
    orders = _orders_create_catalog()
    doc: Dict[str, Any] = {
        **_COD,
        "nodes": [
            {
                "id": "ask",
                "type": "wait_event",
                "topics": ["orders/paid"],
                "key": "$topic",
                "minutes": 60,
                "stage": "paid",
            },
            {
                "id": "thanks",
                "type": "send",
                "channel": "whatsapp",
                "template": "t",
                "variables": {
                    "1": "facts_ask_name",
                    "2": "current_stage",
                    "3": "customer_name",
                },
            },
        ],
        "edges": [["ask", "thanks", "orders/paid"]],
        "purpose_key": "utility.order.thanks",
    }
    catalogs: plans.Catalogs = {"orders/create": orders, "orders/paid": orders}
    assert validate_definition(doc, catalogs=catalogs) == []
    bad = {
        **doc,
        "nodes": [
            doc["nodes"][0],
            {**doc["nodes"][1], "variables": {"1": "facts_ask_confirmed"}},
        ],
    }
    problems = validate_definition(bad, catalogs=catalogs)
    assert any(
        "'facts_ask_confirmed' is not a declared variable field" in p for p in problems
    )
