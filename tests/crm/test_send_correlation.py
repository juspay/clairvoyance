"""The reply join, end to end: an accepted send's wamid reaches ITS run,
and a listening square's `match` lets a reply wake only that run.

The incident this pins (docs/crm/reply-run-matching.md):
one customer, four open COD runs, two button taps — the first tap resolved
all four, cancelling an order she had explicitly confirmed. `_is_about`'s
open default was doing exactly what it documents; what was missing is the
correlate a `match` could compare, because the letter carries Meta's wamid
while the run held only our message id.

The fix is the call square's plant-and-echo pattern with the plant made
late: the dispatcher tells a registered observer when a provider takes a
message, the manifest row carries it (T16 col 14); outreach resolves it onto
the run under provider_message_id_<send node> (the T16 column's
name, one spelling for every channel), and the square says
`match: {payload: replied_to, run: provider_message_id_<node>}`.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import app.crm.connectivity.dispatch as dispatch
from app.crm.outreach.entry import _is_about
from app.crm.outreach.nodes.context import is_bookkeeping, run_facts
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import EnrollmentRun, WorkflowNode
from app.crm.record.schemas import RawEvent
from tests.crm.test_dispatch import _gate_open, _message

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
WAMID = "wamid.HBgMOTE5MzU0MDY5NzgyFQIAERgSRTI2ODIxAA=="


def _workflow_message(run_id: str, node_id: str = "confirm"):
    message = _message()
    return message.model_copy(
        update={
            "source_kind": "workflow",
            "source_id": run_id,
            "dedupe_key": f"{run_id}:{node_id}",
        }
    )


# --- the dispatcher's half: observers hear ACCEPTED, and only accepted -------


async def _dispatch(monkeypatch, message, outcome_status="accepted", wamid=WAMID):
    async def fake_send(send_token, msg):
        """Test double: a send with a scripted outcome."""
        from app.crm.connectivity.schemas.message import SendOutcome

        if outcome_status == "accepted":
            return SendOutcome(status="accepted", provider_message_id=wamid)
        return SendOutcome(status="failed", reason="131026", retryable=False)

    async def record_outcome(*args, **kwargs):
        """Test double: the outcome write succeeds."""
        return True

    monkeypatch.setattr(dispatch, "is_suppressed", _gate_open)
    monkeypatch.setattr(dispatch, "send", fake_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(message, 3)


# --- outreach's half: the correlate on the run ---------------------------------


def test_the_stamp_is_bookkeeping_never_a_template_variable() -> None:
    """An internal id must not resolve into a customer's message; `match`
    reads the context directly, so matching still sees it."""
    assert is_bookkeeping("provider_message_id_confirm")
    assert "provider_message_id_confirm" not in run_facts(
        {"provider_message_id_confirm": WAMID, "name": "#R1"}
    )


# --- the square's half: a reply wakes only ITS run ------------------------------


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m-1000",
        workflow_id=uuid4(),
        workflow_version=7,
        customer_id=uuid4(),
        status="waiting",
        current_node="wait-reply",
        wake_at=NOW,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="order-1",
        attempts=1,
        last_error=None,
    )


def _square(match_run: str = "provider_message_id_confirm") -> WorkflowNode:
    return WorkflowNode(
        id="wait-reply",
        type="wait_event",
        topics=["message.inbound"],
        key="reply",
        minutes=2880,
        match={"payload": "replied_to", "run": match_run},
    )


def _tap(replied_to: Optional[str]) -> RawEvent:
    item: Dict[str, Any] = {
        "from": "919354069782",
        "type": "button",
        "button": {"payload": "CONFIRM", "text": "CONFIRM"},
    }
    if replied_to is not None:
        item["context"] = {"id": replied_to}
    return RawEvent(
        id="ev-1",
        merchant_id="m-1000",
        source="whatsapp",
        topic="message.inbound",
        schema_version="v23.0",
        external_id="wamid.IN",
        payload={"messaging_product": "whatsapp", "messages": [item]},
        received_at=NOW,
        occurred_at=NOW,
    )


def test_a_tap_is_about_the_run_whose_send_it_replies_to() -> None:
    """The incident, inverted: four open runs, one tap — with the stamp and
    the match, the tap claims exactly the run whose message she answered.
    `replied_to` is the catalog's derived read of Meta's context.id, so the
    match exercises the real deriver, not a hand-fed field."""
    hers = _run({"provider_message_id_confirm": WAMID})
    others = [
        _run({"provider_message_id_confirm": f"wamid.OTHER-{n}"}) for n in range(3)
    ]
    tap = _tap(WAMID)
    assert _is_about(_square(), tap, hers) is True
    assert [_is_about(_square(), tap, run) for run in others] == [False] * 3


def test_an_unthreaded_message_claims_nobody() -> None:
    """A fresh message (no context.id) cannot say which order it is about;
    honest silence — the square's alarm, not a guessed edge, recovers."""
    run = _run({"provider_message_id_confirm": WAMID})
    assert _is_about(_square(), _tap(None), run) is False


def test_a_run_whose_send_was_never_accepted_hears_nothing() -> None:
    """No stamp (the send failed, or the acceptance is still in flight) =
    the run claims no reply — it keeps waiting rather than taking a
    letter that may be another run's."""
    run = _run({})
    assert _is_about(_square(), _tap(WAMID), run) is False


# --- publish: a misspelled square name is a sentence, not a silent never -------


def _plan(match_run: str) -> Dict[str, Any]:
    return {
        "entry": {"topic": "orders/create", "key": "id"},
        "nodes": [
            {
                "id": "confirm",
                "type": "send",
                "channel": "whatsapp",
                "template": "t",
                "variables": {},
            },
            {
                "id": "wait-reply",
                "type": "wait_event",
                "topics": ["message.inbound"],
                "key": "reply",
                "minutes": 60,
                "match": {"payload": "replied_to", "run": match_run},
            },
        ],
        "edges": [["confirm", "wait-reply"]],
        "goals": [{"topics": ["orders/cancelled"]}],
        "purpose_key": "utility.order.confirmation",
    }


def test_publish_refuses_a_reply_listen_with_no_match_beside_a_send() -> None:
    """Forgetting the match line silently recreates the incident: the plan
    publishes clean and one customer's answer resolves every open run of
    hers. So a square listening on message.inbound in a plan that SENDS is
    refused until it says whose reply it hears — and the refusal spells the
    exact line to add."""
    plan = _plan("provider_message_id_confirm")
    del plan["nodes"][1]["match"]
    problems = validate_definition(plan)
    assert any(
        "no match" in p and "provider_message_id_confirm" in p for p in problems
    ), problems


def test_a_plan_that_sends_nothing_may_listen_without_a_match() -> None:
    """No send upstream = no reply to mis-route: a plan triggered by ANY
    inbound message (she wrote to us first) is a legitimate open listen."""
    plan = _plan("provider_message_id_confirm")
    del plan["nodes"][1]["match"]
    plan["nodes"] = [n for n in plan["nodes"] if n["type"] != "send"]
    plan["edges"] = []
    plan["entry"]["start"] = "wait-reply"
    problems = validate_definition(plan)
    assert not any("no match" in p for p in problems), problems


def test_publish_refuses_a_provider_id_match_that_names_no_send_node() -> None:
    """provider_message_id_<node> is stamped only for a SEND node's accepted message. A
    typo here would publish clean and then match nothing forever — every
    reply 'claims nobody' — the silent version of the failure match exists
    to prevent."""
    assert validate_definition(_plan("provider_message_id_confirm")) == []
    problems = validate_definition(_plan("provider_message_id_comfirm"))
    assert any("'comfirm' is not a send node" in p for p in problems)
    problems = validate_definition(_plan("provider_message_id_wait-reply"))
    assert any("is not a send node" in p for p in problems)


def test_publish_refuses_a_match_naming_a_send_the_run_has_not_reached() -> None:
    """The quieter half of the same mistake, and the one a real board makes.

    ``chaser`` IS a send and the name is spelled right, so every check above
    passes — but the token has not crossed it when it stands on this square,
    so nothing is ever stamped under that name. The square publishes clean and
    is deaf for as long as it waits: every reply "claims nobody" and the
    timeout edge chases a customer who answered. Only the edge graph can tell
    this apart from the correct plan, which is why the check walks it.
    """
    plan = _plan("provider_message_id_chaser")
    plan["nodes"].append(
        {
            "id": "chaser",
            "type": "send",
            "channel": "whatsapp",
            "template": "t2",
            "variables": {},
        }
    )
    plan["edges"].append(["wait-reply", "chaser", "timeout"])

    problems = validate_definition(plan)

    assert any("has not reached when it stands here" in p for p in problems)
    # And it names the send that WOULD work, so the fix is one edit away.
    assert any("'provider_message_id_confirm'" in p for p in problems)
    # The correct plan over the same nodes still publishes.
    fixed = dict(plan)
    fixed["nodes"] = [
        (
            {
                **n,
                "match": {
                    "payload": "replied_to",
                    "run": "provider_message_id_confirm",
                },
            }
            if n["id"] == "wait-reply"
            else n
        )
        for n in plan["nodes"]
    ]
    assert validate_definition(fixed) == []


# --- the reconcile: the stamp is a cache, the manifest is the truth -----------


def _reconcile(monkeypatch, context, manifest_pmid, stamped: List[tuple]):
    import app.crm.outreach.entry as entry

    async def fake_manifest_read(merchant_id, dedupe_key):
        """Test double: what the manifest row holds for this logical send."""
        stamped.append(("read", merchant_id, dedupe_key))
        return manifest_pmid

    async def fake_stamp(merchant_id, run_id, key, value):
        """Test double: the self-heal write."""
        stamped.append(("stamp", key, value))
        return True

    monkeypatch.setattr(entry, "provider_message_id_for", fake_manifest_read)
    monkeypatch.setattr(entry.enrollment_accessor, "stamp_context_key", fake_stamp)
    run = _run(context)
    patched = asyncio.run(entry._with_send_correlate(run, _square()))
    return run, patched


def test_a_square_resolves_its_send_correlate_from_the_manifest(
    monkeypatch,
) -> None:
    """The stamp can be erased (the walker's advance replaces context whole,
    CAS-guarded on wake_at alone) or never land (the observer's raise is
    swallowed). The manifest row survives both — apply_outcome wrote the id
    before the observer ever ran — so a square about to match and finding
    no stamp reads it back by the send's own dedupe_key, re-stamps, and
    matches. One transient blip can no longer make a run permanently deaf."""
    calls: List[tuple] = []
    run, patched = _reconcile(monkeypatch, {}, WAMID, calls)
    assert patched.context["provider_message_id_confirm"] == WAMID
    assert ("read", "m-1000", f"{run.id}:confirm") in calls
    assert ("stamp", "provider_message_id_confirm", WAMID) in calls
    assert _is_about(_square(), _tap(WAMID), patched) is True


def test_a_correlate_already_on_the_run_costs_no_read(monkeypatch) -> None:
    """The stamp is the fast path: when it is there, the reconcile does not
    touch the database at all — one read per reply per run would put the
    manifest in the consumer's hot loop for nothing."""
    calls: List[tuple] = []
    _, patched = _reconcile(
        monkeypatch, {"provider_message_id_confirm": WAMID}, "ignored", calls
    )
    assert calls == []
    assert patched.context["provider_message_id_confirm"] == WAMID


def test_a_send_never_accepted_still_claims_no_reply(monkeypatch) -> None:
    """No stamp AND no manifest id = the run honestly has nothing a reply
    could echo: it keeps waiting rather than taking another run's letter."""
    calls: List[tuple] = []
    _, patched = _reconcile(monkeypatch, {}, None, calls)
    assert "provider_message_id_confirm" not in patched.context
    assert _is_about(_square(), _tap(WAMID), patched) is False
    assert not any(call[0] == "stamp" for call in calls)


def test_the_reconcile_read_only_trusts_the_post_accept_ladder() -> None:
    """apply_outcome copies outcome.provider_message_id onto FAILED, DEAD
    and QUEUED plans too. No adapter reports an id beside a refusal today,
    but the first that does must not turn a message nobody received into a
    reply correlate — so the read filters to the post-accept ladder in SQL,
    with the words from status.py, never spelled in the query."""
    from app.crm.connectivity.db.queries.message import (
        provider_message_id_for_query,
    )
    from app.crm.connectivity.status import (
        MESSAGE_ACCEPTED,
        MESSAGE_DELIVERED,
        MESSAGE_READ,
        MESSAGE_SENT,
    )

    query, values = provider_message_id_for_query("m-1000", "r:confirm")
    assert "status = ANY($3" in query
    assert values[2] == [
        MESSAGE_ACCEPTED,
        MESSAGE_SENT,
        MESSAGE_DELIVERED,
        MESSAGE_READ,
    ]


def test_a_reconciled_wake_is_not_also_the_runs_repeat(monkeypatch) -> None:
    """_answered_by judges the SAME reconciled run the wake did. open_runs
    was read at the top of the pass, so a stamp the wake just reconciled
    lives on the copy it resumed — judging the stale object here answered
    'not its square's answer' for the very letter that woke the run, and
    apply_repeat re-armed the square it had just resolved."""
    import app.crm.outreach.entry as entry
    from app.crm.outreach.schemas import Workflow, WorkflowDefinition

    run = _run({})  # the stale object: no stamp in memory
    flow = Workflow(
        id=run.workflow_id,
        merchant_id="m-1000",
        name="plan",
        status="live",
        version=7,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=None,
        draft=None,
    )
    plan = _plan("provider_message_id_confirm")
    definition = WorkflowDefinition.model_validate(plan)

    async def fake_definition_for(r):
        """Test double: the run's pinned document."""
        return definition

    async def fake_manifest_read(merchant_id, dedupe_key):
        """Test double: the manifest holds the accepted id."""
        return WAMID

    async def fake_stamp(merchant_id, run_id, key, value):
        """Test double: the self-heal write."""
        return True

    monkeypatch.setattr(entry, "definition_for", fake_definition_for)
    monkeypatch.setattr(entry, "provider_message_id_for", fake_manifest_read)
    monkeypatch.setattr(entry.enrollment_accessor, "stamp_context_key", fake_stamp)

    answered = asyncio.run(
        entry._answered_by([run], flow, run.enrollment_key, _tap(WAMID))
    )
    assert answered is True


# --- publish: the guard is scoped, and both match halves are checked ----------


def test_a_receive_first_square_beside_a_send_branch_is_not_refused() -> None:
    """The refusal walks the EDGE GRAPH, not the node list: a square with
    no send upstream has no reply to mis-route, whatever else the plan
    contains — and the suggested match would be unsatisfiable there (no
    stamp exists before the send)."""
    plan = _plan("provider_message_id_confirm")
    del plan["nodes"][1]["match"]
    # she writes first: listen -> then send. The send is DOWNstream.
    plan["edges"] = [["wait-reply", "confirm"]]
    problems = validate_definition(plan)
    assert not any("no match" in p for p in problems), problems


def test_a_mixed_reply_listen_behind_a_send_is_refused_whole() -> None:
    """A mixed listen downstream of a send is unsafe in BOTH directions —
    matchless it takes every open run's reply, matched it goes deaf on the
    other topic (the match is judged against every letter, and a call
    outcome carries no replied_to). So the shape itself is refused, with
    the fix spelled out: two squares, each saying one thing honestly."""
    plan = _plan("provider_message_id_confirm")
    plan["nodes"][1]["topics"] = ["message.inbound", "call.completed"]

    # matchless AND matched: the same refusal, because neither is safe.
    matched = validate_definition(plan)
    assert any("Split it into two listening squares" in p for p in matched), matched
    del plan["nodes"][1]["match"]
    matchless = validate_definition(plan)
    assert any("Split it into two listening squares" in p for p in matchless), matchless


def test_a_mixed_listen_with_no_send_upstream_stays_legal() -> None:
    """No send upstream = no reply to mis-route on either topic: a
    receive-first square listening broadly is an author's honest choice."""
    plan = _plan("provider_message_id_confirm")
    del plan["nodes"][1]["match"]
    plan["nodes"][1]["topics"] = ["message.inbound", "call.completed"]
    plan["edges"] = [["wait-reply", "confirm"]]  # the send is DOWNstream
    problems = validate_definition(plan)
    assert not any("Split it into two" in p or "no match" in p for p in problems)


def test_the_refusals_suggestion_names_the_upstream_send() -> None:
    """A plan with two sends must not be told to match the wrong one."""
    plan = _plan("provider_message_id_confirm")
    del plan["nodes"][1]["match"]
    plan["nodes"].append(
        {
            "id": "chaser",
            "type": "send",
            "channel": "whatsapp",
            "template": "t2",
            "variables": {},
        }
    )
    # chaser is DOWNstream of the square; only confirm is upstream.
    plan["edges"] = [["confirm", "wait-reply"], ["wait-reply", "chaser"]]
    (problem,) = [p for p in validate_definition(plan) if "no match" in p]
    assert "provider_message_id_confirm" in problem
    assert "provider_message_id_chaser" not in problem


def test_a_typo_inside_the_prefix_is_named_at_publish() -> None:
    """provider_message_confirm (the _id_ dropped) starts with neither
    check's prefix: without this refusal it publishes a square that matches
    nothing forever — indistinguishable from customers not replying."""
    problems = validate_definition(_plan("provider_message_confirm"))
    assert any("did you" in p and "mean" in p for p in problems), problems


def test_a_match_payload_the_topic_never_declared_is_refused() -> None:
    """The mirror of the match.run checks: a typo'd payload field resolves
    to None on every letter and the square is permanently deaf, publish
    clean. Checked against the catalog the caller gathered — the same
    allow-list every other plan word answers to."""
    from app.crm.outreach.catalog_laws import Catalogs
    from app.crm.record.contracts import CatalogField

    catalogs: Catalogs = {
        "orders/create": {},
        "orders/cancelled": {},
        "message.inbound": {
            "replied_to": CatalogField(path="replied_to", type="text", label="R")
        },
    }
    good = validate_definition(_plan("provider_message_id_confirm"), catalogs=catalogs)
    assert not any("match.payload" in p for p in good), good

    plan = _plan("provider_message_id_confirm")
    plan["nodes"][1]["match"]["payload"] = "repplied_to"
    problems = validate_definition(plan, catalogs=catalogs)
    assert any("match.payload" in p and "repplied_to" in p for p in problems)
