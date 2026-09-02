"""W1 publish-validator laws: the exact edit classes canon T19 says the
validator must block, each as a red test."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.plans as plans
from app.crm.outreach.db import DbTxn
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import Workflow

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def _definition(**overrides):
    base = {
        "entry": {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": 0},
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
    "entry": {"topic": "orders/create", "where": {"gateway": "COD"}},
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

    monkeypatch.setattr(plans.accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(plans.accessor, "set_workflow_status", set_workflow_status)
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

    monkeypatch.setattr(plans.accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(plans.accessor, "set_workflow_status", set_workflow_status)
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

    monkeypatch.setattr(plans.accessor, "get_workflow", get_workflow)
    monkeypatch.setattr(plans.accessor, "set_workflow_status", set_workflow_status)
    assert await plans.set_status("m1", "wf-1", "paused") is published
    assert await plans.set_status("m1", "wf-1", "live") is published
    assert writes == ["paused", "live"]


@pytest.mark.asyncio
async def test_unknown_or_archived_plans_answer_none_for_the_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def set_workflow_status(*args: object) -> None:
        raise AssertionError("must not reach the UPDATE")

    monkeypatch.setattr(plans.accessor, "set_workflow_status", set_workflow_status)

    async def missing(merchant_id: str, workflow_id: str) -> None:
        return None

    monkeypatch.setattr(plans.accessor, "get_workflow", missing)
    assert await plans.set_status("m1", "wf-1", "live") is None

    async def archived(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow("archived", None)

    monkeypatch.setattr(plans.accessor, "get_workflow", archived)
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

    async def workflow_for_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow("live", _TERSE_DRAFT).model_copy(update={"draft": self.draft})

    async def occupied_nodes(self, conn: Any, m: str, w: str) -> List[str]:
        return []

    async def apply_publish(self, conn: Any, m: str, w: str) -> Workflow:
        self.published = True
        return _workflow("live", self.draft)

    async def insert_version(self, conn: Any, *args: Any) -> None:
        return None  # phase 11's row; pinned by test_workflow_versions.py

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        return 0


def _registry(
    monkeypatch: pytest.MonkeyPatch, status: Optional[str], registers: bool = True
) -> List[Tuple[str, str, str]]:
    asked: List[Tuple[str, str, str]] = []

    async def template_status(
        merchant_id: str, channel: str, name: str
    ) -> Optional[str]:
        asked.append((merchant_id, channel, name))
        return status

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
    monkeypatch.setattr(plans, "accessor", accessor)
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
    monkeypatch.setattr(plans, "accessor", accessor)
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
    monkeypatch.setattr(plans, "accessor", accessor)
    _registry(monkeypatch, status="approved")
    published = await _publish(accessor)
    assert accessor.published is True and published.status == "live"


@pytest.mark.asyncio
async def test_a_plan_without_send_nodes_never_asks_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_TERSE_DRAFT)
    monkeypatch.setattr(plans, "accessor", accessor)
    asked = _registry(monkeypatch, status=None)
    await _publish(accessor)
    assert asked == [] and accessor.published is True


@pytest.mark.asyncio
async def test_a_channel_that_does_not_register_templates_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_SEND_PLAN)
    monkeypatch.setattr(plans, "accessor", accessor)
    asked = _registry(monkeypatch, status=None, registers=False)
    await _publish(accessor)
    assert asked == [] and accessor.published is True
