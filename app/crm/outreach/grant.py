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
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from app.core.logger import logger
from app.core.logger.context import (
    get_log_context,
    set_log_context,
    update_log_context,
)
from app.crm.outreach import priority, walker
from app.crm.outreach.ceiling import CALLS_TODAY_KEY, max_calls_reached
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    grant as grant_accessor,
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
PAYLOAD_BUDGET_SECONDS = 2.0
LOG_COMPONENT = "crm.outreach.grant"


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


def _holds(context: Dict[str, Any], lead_id: str) -> bool:
    """PURE: a call square of this run wrote this lead (its lead_<node> keys)."""
    return any(k.startswith("lead_") and v == lead_id for k, v in context.items())


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
    again = run.model_copy(update={"context": context})
    token = call.GRANT.set(grant)
    try:
        try:
            await asyncio.wait_for(
                call.execute(again, node, definition), PAYLOAD_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            await call.execute(again, node, _raw(definition))
    finally:
        call.GRANT.reset(token)
    # the run must still stand where the earlier grant left it, right before the insert
    now, _ = await grant_accessor.get_run(str(run.id))
    if (
        now is None
        or now.status != "waiting"
        or now.current_node != run.current_node
        or now.context.get(call._granted_key(node.id)) != lead_id
    ):
        return None
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
    previous = get_log_context()
    set_log_context(component=LOG_COMPONENT, run_id=run_id, lead_id=lead_id)
    try:
        return await _materialize(run_id, lead_id)
    except Exception as e:
        logger.opt(exception=e).error(
            f"grant: run {run_id} lead {lead_id} on {number_id} failed"
        )
        return Refusal.ERROR
    finally:
        set_log_context(**previous)


async def _materialize(run_id: str, lead_id: str) -> Union[str, Refusal]:
    run, plan = await grant_accessor.get_run(run_id)
    if run is None:
        return Refusal.EXITED
    update_log_context(merchant_id=run.merchant_id, workflow_id=str(run.workflow_id))
    if _holds(run.context, lead_id):
        # An earlier grant already put this lead on the run. It is minted or
        # handed out only while the run still stands where that grant left it.
        definition = await definition_for(run)
        placed = call.placed_calls(run, definition) if definition else {}
        node = placed.get(lead_id)
        if (
            list(placed) != [lead_id]
            or node is None
            or run.context.get(call._granted_key(node.id)) != lead_id
        ):
            return Refusal.MOVED  # a lead made today's way is never handed out
        status = await get_lead_status(lead_id)
        if status is None and plan != "live":
            return Refusal.PAUSED if plan == "paused" else Refusal.EXITED
        if status is None:
            try:
                status = await _mint_again(run, lead_id)
            except NodeParked:
                return Refusal.MOVED
        return lead_id if status == LeadCallStatus.BACKLOG else Refusal.MOVED
    if plan != "live":  # resume or archive wakes the run (plans.set_status)
        return Refusal.PAUSED if plan == "paused" else Refusal.EXITED
    if run.status != "waiting" or run.wake_at is None:
        return Refusal.EXITED if run.status == "exited" else Refusal.MOVED
    definition = await definition_for(run)
    if definition is None or _awaited_lead_id(run, definition) != lead_id:
        return Refusal.MOVED

    lease = run.wake_at
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

    after, _ = await grant_accessor.get_run(run_id)
    # a capped visit writes no grant mark; any other needs it: never a lead made today's way
    took = f"lead_{run.current_node}" if capped else call._granted_key(run.current_node)
    if (
        after is not None
        and after.status == "waiting"
        and after.context.get(took) == lead_id
    ):
        await _mint(grant)  # the run took it: only now does the lead exist
        if grant.get("adopted", LeadCallStatus.BACKLOG) != LeadCallStatus.BACKLOG:
            return Refusal.MOVED  # an existing row, already dialled or finished
        return Refusal.CAPPED if capped else lead_id
    # The run did not take the lead: it exited, parked, or an event moved it
    # first. Nothing was minted for it.
    if after is not None and _awaited_lead_id(after, definition) == lead_id:
        return Refusal.ERROR  # only its alarm moved under us: ask again
    if after is None or after.status == "exited":
        timed_out = after is None or after.exit_reason == "timed_out"
        return Refusal.EXITED if timed_out else Refusal.GOAL
    return Refusal.MOVED


async def wake_waiting_calls(pairs: List[Tuple[str, str]]) -> int:
    """Of these (run id, lead id) pairs, wake the runs still parked on a call
    square for exactly that lead: the square runs again, and a number that no
    longer takes parked calls gets its lead made today's way, once."""
    runs = {str(r.id): r for r in await grant_accessor.get_runs([r for r, _ in pairs])}
    parked = [
        (run_id, runs[run_id].current_node)
        for run_id, lead_id in pairs
        if run_id in runs
        and _awaited_lead_id(runs[run_id], await definition_for(runs[run_id]))
        == lead_id
    ]
    return await grant_accessor.wake_runs(parked) if parked else 0


async def wake_parked_calls_after_loss() -> int:
    """The line queue lost its entries (Redis lost them): wake every parked call
    of a live plan; the walker queues each again at its stored place."""
    woken = await grant_accessor.wake_parked_calls_after_loss()
    logger.warning(f"grant: {woken} parked calls woken after the queue was lost")
    return woken


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
        if template not in hours:
            hours[template] = await call.hours_config(definition, template)
        rank = priority.rank_for(definition, run.context, call._now(), hours[template])
        if rank is not None:
            ranks[lead_id] = rank
    return ranks
