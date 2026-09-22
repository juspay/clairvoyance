"""A waiting call square that also hears the merchant (22 Sep 2026;
docs/crm/runbooks/awaiting-call.md §A new event beats a queued call).

Pinned here against the real engine with a fake accessor slice:

- a call square with `event_name` may list the merchant's `topics`,
  judged by `match` like a listening wait; a letter on one aborts the
  queued call if still undialled and follows the topic's arrow;
- the backstop aborts the unplaced call; INVALID_PHONE and BLACKLISTED
  end the run; every other failed outcome takes the plain edge;
- a run that ends early takes its queued calls with it, never a run that
  completed; a dialler retry inherits only the workflow keys;
- two writers on one square: the report yields to a letter already there,
  and an `else` arrow never takes the report or the backstop.
"""

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.db.queries.enrollment import resume_run_by_id_query
from app.crm.outreach.nodes import awaits, listens
from app.crm.outreach.nodes.call import (
    CALL_COMPLETED,
    EJECT_OUTCOMES,
    awaiting_key,
)
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import WorkflowNode
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_queued_leads_by_enrollment_query,
)
from app.schemas import LeadCallStatus
from tests.crm.test_awaiting_call import (  # noqa: F401 — the fixtures ride along
    _LADDER as _PLAIN_LADDER,
    NOW,
    _advance,
    _event,
    _install,
    _queue,
    _run,
    _Writes,
    frozen_clock,
    no_goal,
)

# The ladder, with call-1 listening for OFFERED (an arrow back to quiet).
_LADDER: Dict[str, Any] = {
    **_PLAIN_LADDER,
    "nodes": [
        _PLAIN_LADDER["nodes"][0],
        {**_PLAIN_LADDER["nodes"][1], "topics": ["OFFERED"]},
        *_PLAIN_LADDER["nodes"][2:],
    ],
    "edges": [*_PLAIN_LADDER["edges"], ["call-1", "quiet", "OFFERED"]],
}


# --- the words ---------------------------------------------------------------


def test_a_waiting_call_hears_its_report_first_and_the_topics_it_lists() -> None:
    node = WorkflowNode.model_validate(
        {
            "id": "c",
            "type": "call",
            "template_id": "t",
            "event_name": "call.completed",
            "topics": ["OFFERED"],
            "match": {"run": "customer_id", "payload": "customer_id"},
        }
    )
    assert awaits(node) and listens(node)
    assert node.topics == [CALL_COMPLETED, "OFFERED"] and node.key == "$topic"


def test_the_ladder_publishes_and_the_other_shapes_are_refused() -> None:
    assert validate_definition(_LADDER) == []
    unlisted = {**_LADDER, "edges": [*_LADDER["edges"], ["call-2", "quiet", "KYC"]]}
    assert any("does not list" in p for p in validate_definition(unlisted))
    with_else = {**_LADDER, "edges": [*_LADDER["edges"], ["call-1", "quiet", "else"]]}
    assert validate_definition(with_else) == []  # the author may draw it
    deaf_with_topics = {
        **_LADDER,
        "nodes": [
            _LADDER["nodes"][0],
            {**_LADDER["nodes"][1], "event_name": None},
            *_LADDER["nodes"][2:],
        ],
    }
    assert any(
        "only a call that waits" in p for p in validate_definition(deaf_with_topics)
    )


# --- a letter, the backstop, the outcomes -----------------------------------


def test_a_merchant_letter_supersedes_the_queued_call_and_re_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": "OFFERED",
        },
    )
    _advance(writes, run)
    assert writes.aborts == [(str(run.id), "superseded by OFFERED")]
    # the OFFERED arrow leads back to quiet, which is due inside the hours
    # only after its 15 minutes: the run is armed on quiet
    (moved,) = writes.advances
    assert moved["node"] == "quiet"
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == (
        "call-1",
        "OFFERED",
        "quiet",
    )


def test_the_report_aborts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(
        writes,
        _run(
            "call-1",
            {
                awaiting_key("call-1"): "lead-1",
                "lead_call-1": "lead-1",
                "reply_call-1": CALL_COMPLETED,
                "facts": {"call-1": {"outcome": "NO_ANSWER"}},
            },
        ),
    )
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    assert writes.aborts == []  # the call happened; nothing to abort


def test_the_backstop_aborts_the_unplaced_call_and_takes_the_plain_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run("call-1", {awaiting_key("call-1"): "lead-1", "lead_call-1": "lead-1"})
    _advance(writes, run)
    assert writes.aborts == [(str(run.id), "call call-1 not placed within 60 minutes")]
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert row["outcome"] == "timeout"


@pytest.mark.parametrize("outcome", sorted(EJECT_OUTCOMES))
def test_a_call_that_could_never_be_dialled_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": CALL_COMPLETED,
            "facts": {"call-1": {"outcome": outcome}},
        },
    )
    _advance(writes, run)
    assert writes.advances == []
    (ended,) = writes.exits
    assert ended["reason"] == "ejected"
    (row,) = ended["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == ("call-1", outcome, None)
    assert awaiting_key("call-1") not in ended["context"]
    assert writes.aborts == [(str(run.id), "run exited: ejected")]


@pytest.mark.parametrize(
    "outcome", ["UNKNOWN", "NUMBER_UNAVAILABLE", "NO_CONFIG", "PRECHECK_FAILED"]
)
def test_a_failure_that_is_ours_takes_the_plain_edge_like_a_no_answer(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": CALL_COMPLETED,
            "facts": {"call-1": {"outcome": outcome}},
        },
    )
    _advance(writes, run)
    assert writes.exits == []
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == ("call-1", outcome, "gap")


# --- a run that ends early takes its queued calls with it ---------------------


def test_timed_out_and_ejected_abort_queued_calls_and_completed_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    old = _run("quiet")
    old.entered_at = NOW - timedelta(days=8)
    _advance(writes, old)
    assert writes.aborts == [(str(old.id), "run exited: timed_out")]

    writes = _Writes(_LADDER)
    writes.status = "archived"
    _install(monkeypatch, writes)
    gone = _run("quiet")
    asyncio.run(walker.walk_run(gone))
    assert writes.aborts == [(str(gone.id), "run exited: ejected")]

    # a plan whose last square is a call: completed, and the call stands
    quiet, call = _LADDER["nodes"][0], _LADDER["nodes"][1]
    last_call: Dict[str, Any] = {
        **_LADDER,
        "nodes": [quiet, {**call, "event_name": None, "topics": []}],
        "edges": [["quiet", "call-1"]],
    }
    writes = _Writes(last_call)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(writes, _run("quiet"))
    assert [e["reason"] for e in writes.exits] == ["completed"]
    assert writes.aborts == []


def test_an_exit_the_lease_refused_aborts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER, matched=False)
    _install(monkeypatch, writes)
    old = _run("quiet")
    old.entered_at = NOW - timedelta(days=8)
    _advance(writes, old)
    assert writes.aborts == []


def test_the_abort_names_only_the_runs_queued_undialled_leads() -> None:
    text, values = abort_queued_leads_by_enrollment_query(
        "run-1", "run exited: goal_met"
    )
    assert '"enrollment_id" = $4' in text
    assert '"status" IN ($5, $6)' in text
    assert '"is_locked" = FALSE' in text  # the dialler holds it: already dialling
    assert "run-1" not in text
    status, outcome, meta, run_id, backlog, retry = values
    assert (status, outcome, run_id) == (
        LeadCallStatus.FINISHED.value,
        "ABORT",
        "run-1",
    )
    assert (backlog, retry) == (
        LeadCallStatus.BACKLOG.value,
        LeadCallStatus.RETRY.value,
    )
    assert '"abort_reason": "run exited: goal_met"' in meta


# --- the consumer: a letter is judged by match; the report by the lead ------


def test_a_merchant_letter_on_the_call_square_is_judged_by_match() -> None:
    """Two applications on one phone: the square's `match` keeps the other
    application's letter from aborting this run's call (the cross-over
    guard the plans' listening waits already carry)."""
    node = WorkflowNode.model_validate(
        {
            "id": "c",
            "type": "call",
            "template_id": "t",
            "event_name": "call.completed",
            "topics": ["OFFERED"],
            "match": {"run": "customer_id", "payload": "customer_id"},
        }
    )
    run = _run("c", {"customer_id": "app-A", awaiting_key("c"): "lead-1"})
    mine = _event("OFFERED", {"customer_id": "app-A"})
    theirs = _event("OFFERED", {"customer_id": "app-B"})
    assert entry._is_about(node, mine, run) is True
    assert entry._is_about(node, theirs, run) is False
    # the square's own report still answers to the lead id, not to match
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    assert entry._is_about(node, report, run) is True
    # no match word: every letter on the topic is hers
    plain = WorkflowNode.model_validate(_LADDER["nodes"][1])
    assert entry._is_about(plain, theirs, run) is True


def test_a_letter_on_the_call_square_takes_the_latest_letter() -> None:
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._reply_patch(node, letter, "OFFERED") == {
        "reply_call-1": "OFFERED",
        "cut_short_by": "ev-1",
        "latest_letter": "call-1",
    }


# --- two writers on one square: the report yields, `else` never takes it ------


def test_the_report_and_the_backstop_take_the_plain_edge_before_else() -> None:
    """An `else` arrow on a waiting call answers listed topics the author
    gave no arrow of their own — never the call. Were the `else` scan to
    run first, on any plan with `else` the report and the backstop would
    both leave by `else` and the plain edge would be dead."""
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    arrows: List[Tuple[str, Optional[str]]] = [("gap", None), ("quiet", "else")]
    assert walker.pick_next(node, arrows, {"reply_call-1": CALL_COMPLETED}) == "gap"
    assert walker.pick_next(node, arrows, {}) == "gap"  # the backstop
    assert walker.pick_next(node, arrows, {"reply_call-1": "OFFERED"}) == "quiet"
    labelled = [("quiet", "OFFERED"), *arrows]
    assert walker.pick_next(node, labelled, {"reply_call-1": "OFFERED"}) == "quiet"
    assert walker.pick_next(node, labelled, {"reply_call-1": CALL_COMPLETED}) == "gap"
    # a listed topic with no arrow of its own and no `else`: the plain edge
    assert walker.pick_next(node, [("gap", None)], {"reply_call-1": "OFFERED"}) == "gap"


def test_only_the_waiting_calls_own_report_yields_to_a_letter_on_the_square() -> None:
    call = WorkflowNode.model_validate(_LADDER["nodes"][1])
    listen = WorkflowNode.model_validate(_LADDER["nodes"][4])
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._yields_to(call, report) == "reply_call-1"
    assert entry._yields_to(call, letter) is None  # the latest word replaces
    assert entry._yields_to(listen, letter) is None


def test_the_resume_decides_the_yield_in_the_statement() -> None:
    """A letter heard while the call rang (a ringing lead is not aborted)
    answered the square; the report landing behind it before the walker's
    pass must not replace that answer and those facts. In SQL, so the two
    writers never race."""
    sql, params = resume_run_by_id_query(
        "m1", "run-1", "call-1", {"reply_call-1": CALL_COMPLETED}, {}, "reply_call-1"
    )
    assert "AND ($6::text IS NULL OR NOT (context ? $6::text))" in sql
    assert params[5] == "reply_call-1"
    sql, params = resume_run_by_id_query("m1", "run-1", "call-1", {"reply_call-1": "X"})
    assert params[5] is None  # a merchant letter: unconditional, as before


# --- the dialler side ---------------------------------------------------------


def test_a_retry_inherits_only_the_workflow_keys_and_only_for_a_runs_lead() -> None:
    # The dialler package has an import cycle that its worker resolves first;
    # load it the way the dialler pod does.
    import app.ai.voice.agents.breeze_buddy.dispatch.worker  # noqa: F401
    from app.ai.voice.agents.breeze_buddy.managers.calls import _workflow_meta
    from app.schemas.breeze_buddy.core import LeadCallTracker

    meta = {
        "workflow_id": "wf",
        "enrollment_id": "run",
        "transfer": {"status": "success"},
        "pre_check_defer_count": 3,
    }
    ours = LeadCallTracker.model_construct(enrollment_id="run", metaData=meta)
    theirs = LeadCallTracker.model_construct(enrollment_id=None, metaData=meta)
    assert _workflow_meta(ours) == {"workflow_id": "wf", "enrollment_id": "run"}
    assert _workflow_meta(theirs) == {}
