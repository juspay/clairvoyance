"""simulate — walk a plan against a sample letter, with a fake clock and
no writes (enh A/05).

Merchants publish blind today. A board is a set of squares, clocks and
labelled arrows, and the only way to find out what it does is to publish
it and wait a day for a real customer to walk it. This walks it now, in
one request, and answers the three questions an author actually has: does
my letter get in, what fires and when, and where does the run end.

PURE by construction. Nothing here executes a square: a send, a call and
an action are DESCRIBED — what would go out, with variables and args
resolved from the sample letter's own facts — and the clock is a number
that moves, never a sleep. The only reads are the plan row and its
catalog: no spine letter, no customer, no provider, no run, no write.

The walk reuses the real thing wherever a real thing exists: the publish
validator, the ladder expander, the door's where-grammar, the admission
guard, the condition's predicates, the split's arithmetic, run_facts and
send_variables. A simulator that re-implemented the walker would drift
from it, and the day it drifts is the day it lies to an author about what
will happen.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config.dynamic import CRM_CONTEXT_VALUE_MAX_CHARS
from app.crm.outreach.catalog_laws import gather_catalogs
from app.crm.outreach.db.accessors import workflow as workflow_accessor
from app.crm.outreach.enrol import _admission
from app.crm.outreach.entry import (
    context_from_payload,
    phone_from_payload,
    where_matches,
)
from app.crm.outreach.ladder import LadderProblem, expand_stages
from app.crm.outreach.nodes import NODE_TYPES, is_wait
from app.crm.outreach.nodes.context import reply_key
from app.crm.outreach.nodes.wait_event import ELSE, TIMEOUT
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    SimulateExit,
    SimulateRequest,
    SimulateResult,
    SimulateStep,
    WorkflowDefinition,
    WorkflowEntryAt,
    WorkflowNode,
)
from app.crm.record.contracts import RawEvent

#: How far a dry run will walk. The walker's own bound is per visit; a
#: simulation crosses many, so it needs its own. A board that has not
#: finished in this many squares is a runaway document, and saying so
#: beats looping.
MAX_STEPS = 50

#: The run id the split square is fed. A simulation has no run, and the
#: arm must be the same every time an author presses the button — an
#: answer that changed between two identical requests would read as a bug
#: in the plan rather than as the coin it is. Real runs get real ids and
#: the real spread.
SIMULATED_RUN_ID = "00000000-0000-4000-8000-000000000000"


class SimulationRefused(Exception):
    """The document cannot be walked at all — it does not validate, or the
    plan has nothing to walk. Carries the problems for the route."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


async def simulate(
    merchant_id: str, workflow_id: str, request: SimulateRequest
) -> Optional[SimulateResult]:
    """Walk the plan's live document (or its draft) against one sample
    letter. None when the plan is not this merchant's — the route answers
    404 without saying which of the two it was."""
    workflow = await workflow_accessor.get_workflow(merchant_id, workflow_id)
    if workflow is None:
        return None

    raw = (workflow.draft if request.use_draft else None) or workflow.definition
    if not raw:
        raise SimulationRefused(
            ["this plan has no document to walk — save a draft first"]
        )
    try:
        raw = expand_stages(dict(raw))
    except LadderProblem as e:
        raise SimulationRefused([str(e)]) from e

    catalogs = await gather_catalogs(merchant_id, raw)
    problems = validate_definition(raw, catalogs=catalogs)
    if problems:
        raise SimulationRefused(problems)

    definition = WorkflowDefinition.model_validate(raw)
    event = _sample_event(merchant_id, request)
    now = datetime.now(timezone.utc)

    door, reason = _door_for(definition, event, now)
    if door is None:
        return SimulateResult(admitted=False, reason=reason, path=[], exit=None)

    context = await _context_for(event, request)
    return _walk(definition, door, context, request.answers)


# --- admission ---------------------------------------------------------------


def _sample_event(merchant_id: str, request: SimulateRequest) -> RawEvent:
    """The sample letter as the spine would hold it.

    `source` is the caller's to name and decides ONE thing: whether a
    door's `where` on a DERIVED field resolves (Shopify's
    `fulfillment_state`, WhatsApp's `reply`). Unnamed, those read as
    absent and such a door reads as not matching — honest, and visible in
    the answer. Record's catalog could say who declares a topic, but it
    does not expose it as a contract and widening another module's
    surface is not this step's business; a `source_for_topic` contract is
    owed to track C.
    """
    now = datetime.now(timezone.utc)
    return RawEvent(
        id=SIMULATED_RUN_ID,
        merchant_id=merchant_id,
        source=request.event.source or "",
        topic=request.event.topic,
        schema_version="1",
        external_id="simulated",
        payload=request.event.payload,
        received_at=now,
        occurred_at=now,
    )


def _door_for(
    definition: WorkflowDefinition, event: RawEvent, now: datetime
) -> Tuple[Optional[WorkflowEntryAt], str]:
    """Which door admits this letter, judged as the entry consumer judges
    it: the topic, then the door's own where-grammar, then the admission
    guard for a customer who has never been here.

    A fresh customer is the honest assumption for a dry run: reenter and
    cooldown are about her history, which a simulation does not have and
    must not invent."""
    for door in definition.entries:
        if door.topic != event.topic:
            continue
        if not where_matches(door, event):
            return None, f"the letter does not match the conditions on {door.topic!r}"
        admit, reason = _admission(door, 0, None, now)
        return (door, "admitted") if admit else (None, reason)
    return None, f"no door on this plan starts on {event.topic!r}"


async def _context_for(event: RawEvent, request: SimulateRequest) -> Dict[str, Any]:
    """What the run would carry: the letter's own small facts, the two
    pointers entry.py stamps, then the author's overrides.

    `facts` in the request is how an author asks "what if the cart were
    6,000" without editing the sample payload. It is the one thing here
    not read from the letter, and it is what makes a dry run useful on a
    board that branches."""
    max_chars = await CRM_CONTEXT_VALUE_MAX_CHARS()
    context = context_from_payload(event.payload, max_chars)
    context["source_event_id"] = str(event.id)
    context["entered_event_at"] = (event.occurred_at or event.received_at).isoformat()
    context.update(context_from_payload(request.facts, max_chars))
    # The number the sends would use. It is bookkeeping, so it never
    # survives context_from_payload — entry.py re-adds it from what
    # identity resolved on, and a dry run has no identity, so it reads the
    # letter's own standard keys through the same normalizing helper.
    phone = request.facts.get("phone") or phone_from_payload(event.payload)
    if phone:
        context["phone"] = str(phone)
    # What the split square hashes. Fixed, so two identical requests give
    # one answer; see SIMULATED_RUN_ID.
    context["run_id"] = SIMULATED_RUN_ID
    return context


# --- the walk ----------------------------------------------------------------


def _offset(minutes: float) -> str:
    """PURE: how far into the run this happens, as +HH:MM. Days are hours
    (+26:00, not +1d02:00): a board's clocks are written in minutes and an
    author compares them, so one unit reads better than three."""
    total = int(round(minutes))
    return f"+{total // 60:02d}:{total % 60:02d}"


def _answer_of(
    node: WorkflowNode, context: Dict[str, Any], answers: Dict[str, str]
) -> Optional[str]:
    """What this square answers. A square that decides for itself (a
    condition, a split) is asked through the registry — its own pure half,
    never a copy of it. A listening square is answered by the author's
    `answers` map, and None there means the alarm won."""
    spec = NODE_TYPES[node.type]
    if spec.decide is not None:
        return spec.decide(node, context)
    if spec.listens:
        return answers.get(node.id)
    return None


def _walk(
    definition: WorkflowDefinition,
    door: WorkflowEntryAt,
    context: Dict[str, Any],
    answers: Dict[str, str],
) -> SimulateResult:
    """Square by square on a fake clock, in the walker's own order: the
    square answers, then the arrow is picked, then the answer is spent.

    The clock is minutes since admission, and a step's `at` is when the
    token ARRIVES on that square — so a 30-minute wait reads +00:00 and
    the send after it reads +00:30, which is how an author reads a board.
    A listening square costs its window only when the alarm is what
    answered it: supplying a reply is saying it arrived, and what follows
    happens then rather than a day later.
    """
    nodes = {node.id: node for node in definition.nodes}
    outgoing = definition.outgoing()
    path: List[SimulateStep] = []
    problems: List[str] = []
    elapsed = 0.0
    current: str = door.start

    if door.start not in nodes:
        return SimulateResult(
            admitted=False,
            reason=f"the door starts on {door.start!r}, which is not a square",
            path=[],
            exit=None,
        )

    for _ in range(MAX_STEPS):
        node = nodes[current]  # every edge was validated to name a square
        spec = NODE_TYPES[node.type]
        answer = _answer_of(node, context, answers)
        if spec.branches and answer is not None:
            context[reply_key(node.id)] = answer

        action, problem = _describe(node, context, definition)
        if problem:
            problems.append(f"{node.id}: {problem}")
        path.append(
            SimulateStep(
                node=node.id,
                type=node.type,
                at=_offset(elapsed),
                action=action,
                answer=answer,
            )
        )

        if is_wait(node) and node.minutes and (not spec.listens or answer is None):
            elapsed += node.minutes

        next_id = _pick(node, outgoing.get(node.id, []), answer)
        if spec.branches:
            context.pop(reply_key(node.id), None)
        if next_id is None:
            return SimulateResult(
                admitted=True,
                reason="admitted",
                path=path,
                exit=SimulateExit(reason="completed", at=_offset(elapsed)),
                problems=problems,
            )

        ceiling = definition.exits.max_age_days * 24 * 60
        if elapsed > ceiling:
            return SimulateResult(
                admitted=True,
                reason="admitted",
                path=path,
                exit=SimulateExit(reason="timed_out", at=_offset(ceiling)),
                problems=problems,
            )
        current = next_id

    problems.append(
        f"still walking after {MAX_STEPS} squares — the board loops, or it is "
        "longer than a dry run shows"
    )
    return SimulateResult(
        admitted=True, reason="admitted", path=path, exit=None, problems=problems
    )


def _pick(
    node: WorkflowNode,
    arrows: List[Tuple[str, Optional[str]]],
    answer: Optional[str],
) -> Optional[str]:
    """PURE: the arrow out, on the walker's own rule — the label equal to
    the answer, else `timeout` when the alarm answered, else `else`, else
    the end. Spelled beside the walk it serves and pinned against
    walker.pick_next by a test, because two rules for one board would be
    two boards."""
    if not NODE_TYPES[node.type].branches:
        return arrows[0][0] if arrows else None
    wanted = TIMEOUT if answer is None else answer
    for dst, on in arrows:
        if on == wanted:
            return dst
    for dst, on in arrows:
        if on == ELSE:
            return dst
    return None


def _describe(
    node: WorkflowNode, context: Dict[str, Any], definition: WorkflowDefinition
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """What this square would DO, never doing it — the registry's answer
    (NodeSpec.describe), so a word that fires says what, and a word that
    only waits says nothing.

    Total: a description that cannot resolve becomes a problem instead of
    an action. That refusal is the most useful thing a dry run shows — it
    is the park the author would otherwise meet on a real customer."""
    describe = NODE_TYPES[node.type].describe
    if describe is None:
        return None, None
    try:
        return describe(node, context, definition), None
    except Exception as e:  # noqa: BLE001 — a description never raises
        return None, str(e)
