"""call — a normal buddy lead into today's dispatch machine.

ADR 0010: voice stays outside the gate, governed by its existing checks
(DND, blacklist, calling hours). enrollment_id is stamped after insert (the
050 customer-stamp pattern; the accessor's created hooks give the lead its
customer stamp + lead.pushed mirror for free). Each visit to the square
mints its own lead.
"""

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.outreach import priority
from app.crm.outreach.ceiling import (
    CALLS_TODAY_KEY,
    calls_today,
    max_calls_reached,
    today_on,
)
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.blocks import blocks_for
from app.crm.outreach.nodes.context import (
    OUTCOME_KEY,
    PARK_UNTIL_KEY,
    QUEUE_KEY,
    lead_request_id,
    playbook_key,
    reply_key,
    run_facts,
)
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.nodes.wait import TOPIC_KEY
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.outreach.waiting_calls import (
    WaitingCall,
    call_left,
    call_reranked,
    call_waits,
)
from app.crm.shared import after_commit
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_template_id,
    get_lead_by_id,
    get_lead_status,
    get_template_by_id,
    update_lead_enrollment_id,
    update_waiting_lead_priority,
)
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus

# The ledger and the predicate live in outreach/ceiling.py, not here: a plan
# routes on the same question through `run.max_calls_reached`, so one
# implementation answers both and they can never disagree. The word below is
# this square's alone — what it leaves on its trail row (canon T26 outcome).
MAX_CALLS_OUTCOME = "max_calls"

# The outcome of the lead a capped visit mints: born FINISHED, never dialled.
# Its report (call.completed — the created-lead tap fires it for a lead born
# terminal) reaches the wait after this square the way every call's report
# does, and the run walks on by that wait's own arrows.
ABORTED_OUTCOME = "ABORTED"


# Set by grant.py around the one visit that mints a lead the dialler holds a
# line for: {"lead_id": ...}. The square then leaves its insert under "mint" for
# grant.py to run AFTER the run has moved, so a run an event moved first leaves
# no lead row behind.
GRANT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("crm_call_grant", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    problems = []
    if not node.template_id:
        problems.append(f"call node {node.id} needs a template_id")
    if node.topics and not node.key:
        problems.append(
            f"call node {node.id} lists topics, so it needs a key "
            f"({TOPIC_KEY} to branch on the event's name)"
        )
    return problems


def _visits_key(node_id: str) -> str:
    """Where a call square's visit counter lives in the run's context.

    `lead_` is already a bookkeeping prefix, so run_facts filters it for free
    and it can never reach a template or an action arg.
    """
    return f"lead_visits_{node_id}"


def _visits_so_far(context: Dict[str, Any], node_id: str) -> int:
    """PURE: how many times this run has completed this call square.

    Absent, or unreadable, reads as 0 — so an old run's next id is the `:1`
    it would have had. A wrong count mints a fresh lead; raising here would
    park a run over bookkeeping.
    """
    value = context.get(_visits_key(node_id))
    return value if isinstance(value, int) and value >= 0 else 0


def next_lead_id(run: EnrollmentRun, node_id: str) -> str:
    """PURE: the id of the lead this run's NEXT visit to this call square mints;
    a call waiting for its line is queued under it."""
    visit = _visits_so_far(run.context, node_id) + 1
    return str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run.id}:{node_id}:{visit}"))


def _event_at(rank: Dict[str, Any]) -> Optional[datetime]:
    ms = rank.get("event_ms")
    return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None


def placed_calls(
    run: EnrollmentRun, definition: WorkflowDefinition
) -> Dict[str, WorkflowNode]:
    """PURE: lead id -> its call square, for each call whose placed (unlabelled)
    arrow leads to the square this run waits on: where a grant leaves its run."""
    if run.status != "waiting":
        return {}
    arrows = definition.outgoing()
    return {
        run.context[f"lead_{node.id}"]: node
        for node in definition.nodes
        if f"lead_{node.id}" in run.context
        and (run.current_node, None) in arrows.get(node.id, [])
    }


async def undialled_call(run: EnrollmentRun, definition: WorkflowDefinition) -> bool:
    """True when the run waits right after a call that waited for its line and
    that call's lead is still to be dialled (BACKLOG)."""
    for lead_id, node in placed_calls(run, definition).items():
        if (
            run.context.get(_granted_key(node.id)) == lead_id
            and await get_lead_status(lead_id) == LeadCallStatus.BACKLOG
        ):
            return True
    return False


def _ready_key(node_id: str) -> str:
    """[lead id, ms] of when this square's call was first queued (`lead_` is bookkeeping)."""
    return f"lead_ready_{node_id}"


def _ready_ms(run: EnrollmentRun, node_id: str, lead_id: str) -> Optional[int]:
    """When this call (this lead id) was first queued, if it was."""
    ready = run.context.get(_ready_key(node_id))
    return ready[1] if isinstance(ready, list) and ready[:1] == [lead_id] else None


def _granted_key(node_id: str) -> str:
    """Where a call square records the lead a granted line made (`lead_` is a
    bookkeeping prefix): only such a lead holds its run on the wait after it."""
    return f"lead_granted_{node_id}"


async def withdraw_waiting_call(
    run: EnrollmentRun, definition: Optional[WorkflowDefinition]
) -> None:
    """The run is ending on its square: if that is a call waiting for its
    line, the ask is taken back. Never raises (the hook is fail-open)."""
    nodes = definition.nodes if definition else []
    node = next((n for n in nodes if n.id == run.current_node), None)
    if node is None or node.type != "call" or not node.topics:
        return
    template_id, lead_id = str(node.template_id), next_lead_id(run, node.id)

    async def tell_queue() -> None:  # bounded and fail-open (waiting_calls._call)
        await call_left(template_id, lead_id)

    # the event worker sends it after its transaction commits; anywhere else, now
    if not after_commit.defer(tell_queue):
        await tell_queue()


async def hours_config(definition: WorkflowDefinition, template_id: Any) -> Any:
    """The template's call config for priority.rank_for ("today" is its call hours)."""
    if definition.priority is None:
        return None
    return await get_call_execution_config_by_template_id(str(template_id))


async def rerank_waiting_call(
    run: EnrollmentRun,
    definition: WorkflowDefinition,
    waiting_on: WorkflowNode,
    context: Dict[str, Any],
) -> None:
    """A letter changed this run's rank while its call still waits: write the
    new rank on the lead (taken only while the lead is BACKLOG), then tell the
    queue. The call is the one whose report `waiting_on` listens for: its
    `match` names lead_<call square>. Never raises: a rank must not fail the
    letter that caused it."""
    try:
        # A call square waiting for its line: its queued id has no lead row.
        call: Optional[WorkflowNode] = waiting_on
        lead_id: Any = next_lead_id(run, waiting_on.id)
        if waiting_on.type != "call":
            field = waiting_on.match.run if waiting_on.match else ""
            call = next(
                (
                    n
                    for n in definition.nodes
                    if n.type == "call" and f"lead_{n.id}" == field
                ),
                None,
            )
            lead_id = run.context.get(field)
        if call is None or not lead_id:
            logger.info(f"run {run.id}: no waiting call found from {waiting_on.id}")
            return
        rank = priority.rank_for(
            definition,
            context,
            _now(),
            await hours_config(definition, call.template_id),
        )
        if rank is None:
            return
        if waiting_on.type != "call" and not await update_waiting_lead_priority(
            str(lead_id), rank
        ):
            return
        template_id, new_rank = str(call.template_id), rank

        async def tell_queue() -> None:  # bounded and fail-open (waiting_calls._call)
            await call_reranked(
                template_id,
                str(lead_id),
                new_rank["rank"],
                new_rank["order"],
                _event_at(new_rank),
                next_rank=new_rank.get("next_rank"),
                next_order=new_rank.get("next_order"),
            )

        # the event worker sends it after its transaction commits; anywhere
        # else, now (the lead row, when there is one, has the rank either way)
        if not after_commit.defer(tell_queue):
            await tell_queue()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"run {run.id}: waiting call not re-ranked: {e}")


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Enqueue a lead into today's dispatch machine.

    Idempotent per VISIT: a lease retry re-issues the same insert and the
    existing row is adopted. The accessor turns a duplicate key into None
    like every failure, so the square asks whether its own row is there.

    A square that lists topics waits here for a line instead (PARK_UNTIL_KEY):
    the dialler queues the id this visit would mint and asks grant.py when it
    holds a line; a listed letter meanwhile takes its own arrow.
    """
    if node.topics and run.context.get(reply_key(node.id)) is not None:
        # A listed letter moved the run: its arrow is taken, the ask taken back.
        await call_left(str(node.template_id), next_lead_id(run, node.id))
        return {}
    # The plan's daily ceiling, judged before anything is read (phase 20):
    # at it, this square dials nothing — the lead it mints is born FINISHED
    # with outcome ABORTED (25 Sep 2026), so the row says why no call was
    # made, and the run takes its normal arrow. The ledger is not touched —
    # it counts calls PLACED — so the patch is the same on every re-run of
    # this visit and a lease retry changes nothing. A wait after this
    # square that listens for this call's report hears the aborted lead's
    # report and resolves on ABORTED; a plan may still route past it
    # earlier with a condition on run.max_calls_reached. The report is
    # born at the insert below, one write before the walker moves the run
    # onto that wait: a consumer poll that falls between the two finds no
    # run on the wait and the report is spent — rare, accepted (25 Sep
    # 2026 ruling), and bounded by the wait's own alarm.
    ceiling = definition.exits.max_calls_per_day
    day = today_on(definition.exits)
    if max_calls_reached(run.context, definition.exits):
        logger.bind(
            lead_skip=MAX_CALLS_OUTCOME,
            calls_today=calls_today(run.context, day),
            day=day,
        ).info(
            f"walker: run {run.id} at max_calls_per_day={ceiling} on {day} "
            f"— call square {node.id} places no call"
        )
    capped = max_calls_reached(run.context, definition.exits)

    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"call node {node.id}: no phone in run context")

    template = await get_template_by_id(str(node.template_id))
    if template is None:
        raise NodeParked(f"call node {node.id}: template {node.template_id} not found")
    if template.merchant_id is not None and template.merchant_id != run.merchant_id:
        raise NodeParked(f"call node {node.id}: template belongs to another merchant")
    config = await get_call_execution_config_by_template_id(str(template.id))
    if config is None:
        raise NodeParked(
            f"call node {node.id}: no call_execution_config for template "
            f"{template.name}"
        )

    # Deterministic per (run, node, VISIT). The counter lives in the run's
    # context, so a retry of one visit re-derives its own id and a revisit
    # gets a new one.
    visit = _visits_so_far(run.context, node.id) + 1
    lead_id = next_lead_id(run, node.id)
    grant = GRANT.get() or {}
    granted = grant.get("lead_id") == lead_id  # the dialler holds a line for it

    # A granted call is due now: its waiting already happened.
    next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=0 if granted else config.initial_offset
    )
    rank = priority.rank_for(definition, run.context, _now(), config)
    if node.topics and not capped and not granted:
        asked = rank or {}
        stored = _ready_ms(run, node.id, lead_id)
        request = WaitingCall(
            lead_id=lead_id,
            template_id=str(template.id),
            run_id=str(run.id),
            ready_at=(
                datetime.fromtimestamp(stored / 1000, timezone.utc)
                if stored
                else next_attempt_at
            ),
            rank=asked.get("rank", 0),  # 0: the number's default rank
            order=asked.get("order", "first_ready"),
            event_at=_event_at(asked),
            next_rank=asked.get("next_rank"),
            next_order=asked.get("next_order"),
        )
        max_age = timedelta(days=definition.exits.max_age_days)
        park: Dict[str, Any] = {PARK_UNTIL_KEY: run.entered_at + max_age}
        if stored is None:
            # first queued now; a re-queue of this call keeps this place
            ready_ms = int(next_attempt_at.timestamp() * 1000)
            park[_ready_key(node.id)] = [lead_id, ready_ms]
        if run.current_node != node.id:
            # A grant must find the run here and the move is not written yet:
            # the walker queues the call once it holds the run on this square.
            return {**park, QUEUE_KEY: request}
        if await call_waits(request):  # not taken: made today's way, below
            return park
    # The template-variable bridge: every small fact the entry processor
    # carried (item, cart_value, ...) reaches the agent via the lead
    # payload — {placeholder}s in the template resolve from these keys.
    # reporting_webhook_url rides too: the lead machine reads it from the
    # lead payload to report the call's outcome back to the merchant.
    # The finished blocks ride BESIDE the scalars, into the payload only:
    # the run context has a size ceiling and canon keeps the row small,
    # so a rendered walk never touches it.
    rendered, chosen = await blocks_for(run, node, definition, node.blocks)
    payload: Dict[str, Any] = {
        **run_facts(run.context, node),
        **rendered,
        "customer_mobile_number": phone,
    }
    meta_data: Dict[str, Any] = {
        "workflow_id": str(run.workflow_id),
        "enrollment_id": str(run.id),
    }
    if rank is not None:
        # the dialler reads the call's rank here; ready_ms keeps its place on a re-queue
        ready = _ready_ms(run, node.id, lead_id) if granted else None
        meta_data["priority"] = {**rank, "ready_ms": ready} if ready else rank

    async def mint() -> None:
        assert template is not None
        try:
            lead = await create_lead_call_tracker(
                id=lead_id,
                reseller_id=template.reseller_id,
                template=template.name,
                template_id=str(template.id),
                merchant_id=run.merchant_id,
                next_attempt_at=next_attempt_at,
                payload=payload,
                attempt_count=0,
                meta_data=meta_data,
                request_id=lead_request_id(
                    run.context,
                    str(run.id),
                    (
                        run.enrollment_key
                        if any(door.key for door in definition.entries)
                        else None
                    ),
                ),
                execution_mode=ExecutionMode.TELEPHONY,
                status=LeadCallStatus.FINISHED if capped else LeadCallStatus.BACKLOG,
                outcome=ABORTED_OUTCOME if capped else None,
                call_end_time=datetime.now(timezone.utc) if capped else None,
            )
        except UniqueViolation:
            # Same meaning as None, so it falls to the same lookup. The accessor
            # swallows this today; kept for the day it narrows.
            lead = None

        if lead is None:
            # None means every failure, a duplicate key included, so the row's
            # existence tells them apart: ours means this visit already ran under
            # a lost lease — adopt it. Absent means a real failure.
            lead = await get_lead_by_id(lead_id)
            if lead is None:
                raise RuntimeError(f"call node {node.id}: lead insert returned None")
            if granted and not capped:  # a granted line dials it only if BACKLOG
                grant["adopted"] = lead.status
            logger.bind(lead_id=lead_id).info(
                f"walker: run {run.id} lead {lead_id} already exists "
                f"(lease retry of visit {visit}) — continuing"
            )

        await update_lead_enrollment_id(lead_id, str(run.id))
        # lead_id as a FIELD — the join to Buddy's dial line needs a column.
        logger.bind(lead_id=lead_id).info(
            f"walker: run {run.id} "
            + (
                f"minted aborted lead {lead_id} (node {node.id}, no call placed)"
                if capped
                else f"pushed lead {lead_id} (node {node.id})"
            )
        )

    if granted and not capped:
        grant["mint"] = mint  # the run moves first; grant.py mints after
    else:
        await mint()
    written: Dict[str, Any] = {
        f"lead_{node.id}": lead_id,
        _visits_key(node.id): visit,
    }
    if granted and not capped:
        written[_granted_key(node.id)] = lead_id
    if capped:
        # Not placed: the ledger counts calls PLACED and stays as it is. The
        # trail word rides out; the walker pops it, it never reaches the context.
        written[OUTCOME_KEY] = MAX_CALLS_OUTCOME
    elif ceiling is not None:
        # Re-stamping with today's date IS the reset: a ledger carried over
        # from yesterday is replaced, never added to. Nothing else is written
        # — there is no cached "reached" flag to clear, because the predicate
        # is computed from this ledger every time it is asked. A ceiling taken
        # away mid-run (`max_calls_per_day: null`) therefore frees the run on
        # the next question, with no stale `true` to survive it.
        written[CALLS_TODAY_KEY] = {"day": day, "n": calls_today(run.context, day) + 1}
    if chosen:
        written[playbook_key(node.id)] = chosen
    return written
