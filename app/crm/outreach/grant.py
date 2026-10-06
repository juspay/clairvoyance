"""A workflow call becomes a lead only when the dialler grants it a line.

The dialler queues the id the call square WOULD mint, reserves a line for
it, and then asks here. One visit of the walker does the work with the code
that already exists: the exits (max age, goal), the call square's own mint
(the payload from the run as it is NOW, the insert, an adopted duplicate,
the daily ceiling) and the move to the square after it — all conditional on
the wake_at read here. This file adds only the question and the answers.

The run moves FIRST and the lead is inserted after (nodes/call.py GRANT): a
run an event moved meanwhile leaves no lead row at all. A grant that dies
between the two is repaired by the next one: the run already carries the id,
so the lead is minted then, but only while the run still stands where that
grant left it. Once it has moved past, the id is refused.

Never raises, and never leaves a dialable lead behind a run that moved on.
"""

import asyncio
import inspect
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from app.core.config import static
from app.core.logger import logger
from app.crm.outreach import priority, walker
from app.crm.outreach.ceiling import CALLS_TODAY_KEY, max_calls_reached
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    grant as grant_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.nodes import call
from app.crm.outreach.nodes.call import _visits_key, _visits_so_far, next_lead_id
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition
from app.database.accessor import get_lead_status
from app.schemas.breeze_buddy.core import LeadCallStatus
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY

# How long the mint may take while a line is held. Past it the payload is
# built again from the raw values (no llm_call).
PAYLOAD_BUDGET_SECONDS = float(getattr(static, "CRM_GRANT_PAYLOAD_BUDGET_SECONDS", 2.0))


class Refusal(str, Enum):
    """Why no call is placed. ERROR means "ask again"; the rest are final."""

    MOVED = "moved"
    EXITED = "exited"
    GOAL = "goal"
    CAPPED = "capped"
    PAUSED = "paused"
    ERROR = "error"


def _awaited_lead_id(
    run: EnrollmentRun, definition: Optional[WorkflowDefinition]
) -> Optional[str]:
    """PURE: the id of the lead this run is waiting on a call square for —
    the id nodes/call.py mints for its next visit — or None."""
    if run.status != "waiting" or definition is None:
        return None
    node = next((n for n in definition.nodes if n.id == run.current_node), None)
    if node is None or node.type != "call":
        return None
    return next_lead_id(run, node.id)


async def _mint(grant: Dict[str, Any]) -> None:
    """The lead a granted visit left for after the move (nodes/call.py GRANT)."""
    if "mint" in grant:
        await grant["mint"]()


async def _mint_again(run: EnrollmentRun, lead_id: str) -> Any:
    """An earlier grant moved the run on with this lead and died before its
    insert: mint it now, from the run as it stands. The call square derives
    the id from the visit BEFORE the one the run counted, and this call is
    already on the day's ledger."""
    definition = await definition_for(run)
    nodes = definition.nodes if definition else []
    node = next((n for n in nodes if run.context.get(f"lead_{n.id}") == lead_id), None)
    if node is None or definition is None:
        return None
    visits = {_visits_key(node.id): _visits_so_far(run.context, node.id) - 1}
    context = {**run.context, **visits}
    context.pop(CALLS_TODAY_KEY, None)
    grant: Dict[str, Any] = {"lead_id": lead_id}
    token = call.GRANT.set(grant)
    try:
        await call.execute(
            run.model_copy(update={"context": context}), node, definition
        )
    finally:
        call.GRANT.reset(token)
    await _mint(grant)
    return await get_lead_status(lead_id)


def _raw(definition: WorkflowDefinition) -> WorkflowDefinition:
    """PURE: the plan with every awaited built-in (llm_call) taken out of its
    transforms, so a line renders at once from the value as it arrived."""
    playbook = definition.playbook
    if playbook is None:
        return definition
    transform = {
        fact: how.model_copy(
            update={
                "function": [
                    name
                    for name in how.function
                    if not inspect.iscoroutinefunction(
                        TEMPLATE_FUNCTION_REGISTRY.get(name)
                    )
                ]
            }
        )
        for fact, how in playbook.transform.items()
    }
    return definition.model_copy(
        update={"playbook": playbook.model_copy(update={"transform": transform})}
    )


async def materialize_call(
    run_id: str, lead_id: str, number_id: str
) -> Union[str, Refusal]:
    """A line on ``number_id`` is held for ``lead_id``: mint the lead and
    move the run on. Returns the lead id (its row is BACKLOG, dial it) or the
    reason nothing is to be dialled. Safe to call again with the same ids."""
    try:
        return await _materialize(run_id, lead_id)
    except Exception as e:
        logger.error(
            f"grant: run {run_id} lead {lead_id} on {number_id} failed — "
            f"{type(e).__name__}: {e}"
        )
        return Refusal.ERROR


async def _materialize(run_id: str, lead_id: str) -> Union[str, Refusal]:
    runs = await grant_accessor.get_runs([run_id])
    if not runs:
        return Refusal.EXITED
    run = runs[0]
    if lead_id in run.context.values():
        # An earlier grant already put this lead on the run. It is minted or
        # handed out only while the run still stands where that grant left it.
        definition = await definition_for(run)
        if definition is None or list(call.placed_calls(run, definition)) != [lead_id]:
            return Refusal.MOVED
        status = await get_lead_status(lead_id)
        if status is None:
            status = await _mint_again(run, lead_id)
        return lead_id if status == LeadCallStatus.BACKLOG else Refusal.MOVED
    if run.status != "waiting" or run.wake_at is None:
        return Refusal.EXITED if run.status == "exited" else Refusal.MOVED
    plan = await workflow_accessor.workflow_status(
        run.merchant_id, str(run.workflow_id)
    )
    if plan == "paused":
        return Refusal.PAUSED
    if plan is None or plan == "archived":
        return Refusal.EXITED
    definition = await definition_for(run)
    if definition is None or _awaited_lead_id(run, definition) != lead_id:
        return Refusal.MOVED

    lease = run.wake_at
    status = await get_lead_status(lead_id)
    if status is not None and status != LeadCallStatus.BACKLOG:
        # The id is spent and can never be dialled: the visit is counted up
        # and the run woken, so the walker queues it under its next id.
        node_id = run.current_node
        visits = {_visits_key(node_id): _visits_so_far(run.context, node_id) + 1}
        await enrollment_accessor.advance_run(
            run_id,
            node_id,
            datetime.now(timezone.utc),
            {**run.context, **visits},
            lease,
        )
        return Refusal.MOVED

    capped = max_calls_reached(run.context, definition.exits)
    grant: Dict[str, Any] = {"lead_id": lead_id}
    token = call.GRANT.set(grant)
    try:
        try:
            await asyncio.wait_for(
                walker._advance(run, definition, lease), PAYLOAD_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            await walker._advance(run, _raw(definition), lease)
    except NodeParked as e:
        await enrollment_accessor.park_run(run_id, str(e), lease)
    finally:
        call.GRANT.reset(token)

    again = await grant_accessor.get_runs([run_id])
    after = again[0] if again else None
    if after is not None and lead_id in after.context.values():
        await _mint(grant)  # the run took it: only now does the lead exist
        return Refusal.CAPPED if capped else lead_id
    # The run did not take the lead: it exited, parked, or an event moved it
    # first. Nothing was minted for it.
    if after is not None and _awaited_lead_id(after, definition) == lead_id:
        return Refusal.ERROR  # only its alarm moved under us: ask again
    if after is None or after.status == "exited":
        timed_out = after is None or after.exit_reason == "timed_out"
        return Refusal.EXITED if timed_out else Refusal.GOAL
    return Refusal.MOVED


async def calls_still_wanted(pairs: List[Tuple[str, str]]) -> Set[str]:
    """Of these (run id, lead id) pairs, the lead ids whose run still waits
    on a call square for exactly that lead — what a queue cleanup may keep."""
    runs = {
        str(run.id): run
        for run in await grant_accessor.get_runs([run_id for run_id, _ in pairs])
    }
    wanted: Set[str] = set()
    for run_id, lead_id in pairs:
        run = runs.get(run_id)
        if run is not None and (
            _awaited_lead_id(run, await definition_for(run)) == lead_id
        ):
            wanted.add(lead_id)
    return wanted


async def ranks_for_leads(pairs: List[Tuple[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Of these (lead id, run id) pairs, the rank each lead has NOW, judged
    from its run: {lead id: {rank, order, event_ms[, next_rank, next_order]}}
    (priority.rank_for). Left out: a lead whose run is gone or has exited,
    whose plan declares no `priority`, or that is not the lead one of the
    run's call squares queued. Reads only: the runs in one read, and a
    template's call hours once."""
    runs = {
        str(run.id): run
        for run in await grant_accessor.get_runs([run_id for _, run_id in pairs])
    }
    hours: Dict[str, Any] = {}
    ranks: Dict[str, Dict[str, Any]] = {}
    for lead_id, run_id in pairs:
        run = runs.get(run_id)
        if run is None or run.status == "exited":
            continue
        definition = await definition_for(run)
        queued_by = next(
            (
                n
                for n in (definition.nodes if definition else [])
                if n.type == "call" and run.context.get(f"lead_{n.id}") == lead_id
            ),
            None,
        )
        if definition is None or queued_by is None:
            continue
        template = str(queued_by.template_id)
        if hours.get(template) is None:  # None also when a plan names its own window
            hours[template] = await call.hours_config(definition, template)
        rank = priority.rank_for(definition, run.context, call._now(), hours[template])
        if rank is not None:
            ranks[lead_id] = rank
    return ranks


async def waiting_calls_page(
    after: Optional[Tuple[Any, str]], limit: int
) -> Tuple[List[Dict[str, Any]], Optional[Tuple[Any, str]]]:
    """A page of the calls waiting for a line with no lead row yet: the runs
    holding on a call square that lists topics, of plans that are not paused.
    Each is {run_id, lead_id, template_id, priority}: the lead id that square
    queued (nodes/call.py) and the rank it has now (None: the plan declares
    none). Returns (calls, the `after` of the next page); `after` is None for
    the first page and comes back None on the last. A page may hold fewer
    calls than `limit`. Reads only."""
    merchants, workflows, nodes = await grant_accessor.parking_squares()
    runs = (
        await grant_accessor.waiting_runs_page(
            merchants, workflows, nodes, after, limit
        )
        if nodes
        else []
    )
    hours: Dict[str, Any] = {}
    calls: List[Dict[str, Any]] = []
    for run in runs:
        definition = await definition_for(run)
        node = next(
            (
                n
                for n in (definition.nodes if definition else [])
                if n.id == run.current_node
            ),
            None,
        )
        if definition is None or node is None or node.type != "call" or not node.topics:
            continue
        template = str(node.template_id)
        if hours.get(template) is None:  # None also when a plan names its own window
            hours[template] = await call.hours_config(definition, template)
        calls.append(
            {
                "run_id": str(run.id),
                "lead_id": next_lead_id(run, node.id),
                "template_id": template,
                "priority": priority.rank_for(
                    definition, run.context, call._now(), hours[template]
                ),
            }
        )
    more = len(runs) == limit
    return calls, (runs[-1].wake_at, str(runs[-1].id)) if more else None
