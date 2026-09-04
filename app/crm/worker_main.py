"""The only registry of CRM worker roles — closed dict, no discovery. Lives
at the package root because it is the one leaf that may import every
module's contracts side by side."""

import asyncio
from typing import Any, Callable, Coroutine, Dict, Optional

from app.core.config.static import (
    CRM_DISPATCH_BATCH,
    CRM_WORKER_BATCH,
    CRM_WORKER_INTERVAL,
    POSTGRES_MAX_OVERFLOW,
    POSTGRES_POOL_SIZE,
)
from app.crm.connectivity.contracts import (
    claim_sends,
    consume_template_event,
    dispatch_send,
    register_retire_guard,
)
from app.crm.outreach.contracts import (
    claim_due_runs,
    consume_attributed_event,
    template_references,
    walk_run,
)
from app.crm.record.consumers import register_consumer
from app.crm.record.workers import observe_processed_event, run_pass
from app.crm.shared.worker import run_drain_loop

# The composition root fills record's consumer slot: record owns the WHEN
# (per row, inside the row's savepoint), this file owns the WHO — so the
# import always runs subscriber -> record, never back (checker rule 12).
# Segments and the transactional-send consumer (A13) each add one line here.
register_consumer(consume_attributed_event)
# The second subscriber, and the reason record's letters carry no filter:
# connectivity's template consumer is only ever interested in MERCHANT-level
# letters (a template review names no person), which the entry-rules
# consumer above returns from at once. Registration order is execution
# order; these two never look at the same letter.
register_consumer(consume_template_event)
# The same inversion for connectivity's template retire guard (phase 14):
# connectivity may not import outreach, so this root hands outreach's
# "who would still send this template" into connectivity's slot.
# app/main.py imports this module, so the API pod (where the retire route
# lives) is wired too.
register_retire_guard(template_references)

ROLES: Dict[str, Callable[[asyncio.Event], Coroutine[Any, Any, None]]] = {
    "event-worker": lambda stop_event: run_drain_loop(
        run_pass,
        observe_processed_event,
        interval=CRM_WORKER_INTERVAL,
        batch=CRM_WORKER_BATCH,
        stop_event=stop_event,
        name="event-worker",
    ),
    "dispatcher": lambda stop_event: run_drain_loop(
        claim_sends,
        dispatch_send,
        interval=CRM_WORKER_INTERVAL,
        # Its own dial, not CRM_WORKER_BATCH: a claimed batch must finish
        # inside the claim lease (batch x send timeout < lease), or another
        # pod's sweep re-sends the unworked tail — a real duplicate message.
        batch=CRM_DISPATCH_BATCH,
        stop_event=stop_event,
        name="dispatcher",
    ),
    "walker": lambda stop_event: run_drain_loop(
        claim_due_runs,
        walk_run,
        interval=CRM_WORKER_INTERVAL,
        batch=CRM_WORKER_BATCH,
        stop_event=stop_event,
        name="walker",
    ),
}

_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


async def start_worker_role(role: str) -> None:
    """No-op for role == 'api'. Raises on an unregistered role, or on a pool
    too small for a pass to make progress in."""
    global _task, _stop_event
    if role == "api" or _task is not None:
        return
    if role not in ROLES:
        raise ValueError(f"unknown CRM_ROLE: {role!r}")
    ceiling = POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW
    if ceiling < 2:
        raise RuntimeError(
            f"CRM_ROLE={role!r} needs a DB pool ceiling of at least 2, got "
            f"{ceiling} (POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW). A pass "
            f"holds two at once: #1 carries the claim's transaction for the "
            f"whole batch (FOR UPDATE SKIP LOCKED releases only at commit), "
            f"and #2 is taken and returned by each contract the rows call — "
            f"resolve(), then assert_facts() — since each opens its own "
            f"transaction. At a ceiling of one, #2 waits on the connection #1 "
            f"is holding and the worker hangs on the first row, silently."
        )
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(ROLES[role](_stop_event), name=f"crm-{role}")


async def stop_worker_role() -> None:
    global _task, _stop_event
    if _task is None or _stop_event is None:
        return
    _stop_event.set()
    try:
        await asyncio.wait_for(_task, timeout=10)
    except asyncio.TimeoutError:
        _task.cancel()
    _task = None
    _stop_event = None
