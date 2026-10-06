"""
Created-lead hook: put workflow leads on the dispatch schedule at creation.

The CRM workflow call node inserts its lead as BACKLOG and returns; without
this the lead waits for ``reconcile_backlog_to_zset`` (up to 60 s) to be
scheduled. app/crm cannot import app.ai, so the node is untouched and the
schedule happens from the data layer's created-lead hook instead.

The accessor fires hooks synchronously, in the event loop, right after the
INSERT returned (autocommit, so the row is already visible). The hook only
spawns a background task, so lead creation never waits on Redis, and nothing
raised here reaches the caller. Best-effort by design: the DB row is
authoritative and the backlog reconciler still heals any miss.

Only for a lead whose number is on v2: there it goes straight into its
template's room. Every other lead is left exactly as today (today's backlog
reconciler schedules it), so deploying this with v2 off, or with v2 on for
some numbers only, changes nothing for the rest.
"""

from __future__ import annotations

from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    is_dispatchable,
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import v2_seen
from app.core.concurrency import spawn_background_task
from app.core.logger import logger
from app.database.accessor.breeze_buddy import lead_call_tracker as lct_accessor
from app.schemas import LeadCallStatus, LeadCallTracker


async def _on_v2_number(template_id: Optional[str]) -> bool:
    """Is this template's number on v2 right now? False while v2 was never used
    (no read at all), for today's numbers, and when the mode can't be read."""
    if not template_id or not await v2_seen():
        return False
    # lazy: routes -> managers.calls -> dispatch (import cycle)
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
        template_is_v2_accounted,
    )

    return await template_is_v2_accounted(template_id) is True


async def _schedule_created_lead(lead: LeadCallTracker) -> None:
    # Capped workflow leads are born FINISHED (no call placed), so the BACKLOG
    # check is what keeps them off the schedule.
    if (
        lead.status != LeadCallStatus.BACKLOG
        or not (lead.metaData or {}).get("workflow_id")
        or lead.next_attempt_at is None
        or not is_dispatchable(lead.execution_mode)
    ):
        return
    if not await _on_v2_number(lead.template_id):
        return  # today's path, unchanged: the backlog reconciler schedules it
    await schedule_lead(
        str(lead.id), lead.next_attempt_at, template_id=lead.template_id
    )


def _created_lead_hook(lead: LeadCallTracker) -> None:
    try:
        spawn_background_task(
            _schedule_created_lead(lead), name=f"schedule-created-lead-{lead.id}"
        )
    except Exception as e:  # fail-open: never break lead creation
        logger.error(f"schedule created-lead hook failed for {lead.id}: {e}")


# Install the hook. Idempotent: the registry ignores re-registration.
lct_accessor.register_created_hook(_created_lead_hook)
