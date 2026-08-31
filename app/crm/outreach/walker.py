"""The walker (W3) — the clock that moves tokens. NOT an engine (canon:
"wake_at + the live document IS the engine"): claim due runs off the
partial index, read the plan LIVE, execute the current node, write the
next alarm. Correctness rides the wake_at lease + idempotent writes,
never worker uniqueness — scale is replicas.

The node vocabulary — what each square does when the token lands, and
whether landing means waiting — lives in nodes.py (NODE_TYPES); the walker
dispatches through it and never matches a type string.

The goal is re-checked at every claim BEFORE acting (canon: never "did
you forget?" to someone who just paid) — the entry processor's
goal-cancel is the fast path; this is the belt-and-suspenders.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config.static import (
    CRM_WALKER_LEASE_SECONDS,
    CRM_WALKER_MAX_ATTEMPTS,
)
from app.core.logger import logger
from app.crm.outreach.db import accessor
from app.crm.outreach.entry import reply_key
from app.crm.outreach.nodes import NODE_TYPES, NodeParked, is_wait
from app.crm.outreach.plans import TIMEOUT
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.record.contracts import customer_has_event

# One claim executes consecutive immediate nodes (call -> next wait) in a
# single visit; the bound is a runaway-document guard, not a feature.
_MAX_STEPS_PER_VISIT = 10

# Transient-failure retry: exponential from the lease, capped, ±20% jitter
# (canon T20: "backoff with jitter written into wake_at").
_RETRY_CAP_SECONDS = 3600


def retry_delay_seconds(attempts: int, base: int) -> int:
    """PURE: how long the next retry waits. attempts already counts this
    claim, so the first retry waits one base."""
    delay = min(base * 2 ** max(attempts - 1, 0), _RETRY_CAP_SECONDS)
    return max(1, round(delay * random.uniform(0.8, 1.2)))


async def claim_due_runs(batch: int) -> List[EnrollmentRun]:
    """The walker's claim for the drain loop (worker-runtime.md): the
    wake_at lease push — one UPDATE that moves the alarm one lease window
    forward IS the lock, so replicas never collide and a crashed claim
    self-heals when the pushed alarm comes due again."""
    return await accessor.claim_due_runs(batch, CRM_WALKER_LEASE_SECONDS)


async def walk_run(run: EnrollmentRun) -> None:
    """Move one claimed token as far as it can go this visit. The claim
    already pushed wake_at one lease window, so every failure path below
    retries by simply doing nothing — the clock brings the run back."""
    try:
        workflow = await accessor.get_workflow(run.merchant_id, str(run.workflow_id))
        if workflow is None or workflow.status == "archived":
            await accessor.exit_run(str(run.id), "ejected")
            return
        if workflow.status == "paused" or not workflow.definition:
            return  # the lease push IS the snooze; re-checked next wake
        definition = WorkflowDefinition.model_validate(workflow.definition)
        await _advance(run, definition)
    except NodeParked as e:
        await accessor.park_run(str(run.id), str(e))
        logger.warning(f"walker: run {run.id} parked — {e}")
    except Exception as e:
        if run.attempts >= CRM_WALKER_MAX_ATTEMPTS:
            await accessor.park_run(str(run.id), f"attempts exhausted: {e}")
            logger.error(f"walker: run {run.id} parked after retries — {e}")
        else:
            retry_in = retry_delay_seconds(run.attempts, CRM_WALKER_LEASE_SECONDS)
            await accessor.record_run_error(str(run.id), str(e), retry_in)
            logger.warning(f"walker: run {run.id} retries in {retry_in}s — {e}")


async def _advance(run: EnrollmentRun, definition: WorkflowDefinition) -> None:
    nodes = {node.id: node for node in definition.nodes}
    outgoing = definition.outgoing()
    now = datetime.now(timezone.utc)

    # The hard ceiling first: a run older than the plan's max age exits
    # timed_out no matter which square it stands on.
    max_age = timedelta(days=definition.exits.max_age_days)
    if now - run.entered_at > max_age:
        await accessor.exit_run(str(run.id), "timed_out")
        return

    # Goal re-check at fire time — one indexed EXISTS via record's
    # contract, never a foreign SELECT.
    if await customer_has_event(
        run.merchant_id,
        str(run.customer_id),
        definition.goal.topics,
        run.entered_at,
    ):
        await accessor.exit_run(str(run.id), "goal_met")
        return

    current_id = run.current_node
    context = dict(run.context)
    for _ in range(_MAX_STEPS_PER_VISIT):
        node = nodes.get(current_id)
        if node is None:
            # The publish validator forbids stranding, so this is drift
            # (e.g. a run parked across an archive/re-create) — honest park.
            raise NodeParked(f"node {current_id} not in live definition")

        execute = NODE_TYPES[node.type].execute
        if execute is not None:  # a wait's action IS the alarm
            context.update(await execute(run, node, definition))

        next_id = pick_next(node, outgoing.get(current_id, []), context)
        if next_id is None:
            await accessor.exit_run(
                str(run.id), "completed", current_node=current_id, context=context
            )
            return

        next_node = nodes.get(next_id)
        if next_node is None:
            raise NodeParked(f"edge points at unknown node {next_id}")
        if is_wait(next_node) and next_node.minutes:
            # Arrival scheduling: the wait's alarm starts now.
            await accessor.advance_run(
                str(run.id),
                next_id,
                datetime.now(timezone.utc) + timedelta(minutes=next_node.minutes),
                context,
            )
            return
        current_id = next_id  # action node: execute in this same visit

    raise NodeParked(
        f"{_MAX_STEPS_PER_VISIT} immediate nodes in one visit — runaway document"
    )


def pick_next(
    node: WorkflowNode, arrows: List[Tuple[str, Optional[str]]], context: Dict[str, Any]
) -> Optional[str]:
    """PURE: which arrow leaves this square. A plain node has one. A
    wait_event node takes the arrow labelled with its answer, or
    "timeout" when the alarm fired first; no matching arrow = the end."""
    if node.type != "wait_event":
        return arrows[0][0] if arrows else None
    answer = context.get(reply_key(node.id))
    wanted = TIMEOUT if answer is None else answer
    for dst, on in arrows:
        if on == wanted:
            return dst
    return None
