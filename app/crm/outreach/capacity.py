"""capacity — the lines a call template dials through, and how many cold
runs a plan may be handed this pass.

The overnight drain (21 Sep 2026; docs/crm/runbooks/overnight-drain.md).
A cold run is one a calling window held overnight; its call is the pile's,
not a customer acting now. The walker claims hot runs first, unbounded,
then cold runs oldest-held-first — and a cold run's CALL is written only
while its number has a free line. That decision is made where the lead is
written (nodes/call.py: one INSERT whose condition is the count of calls
already holding the number's lines), never from a count taken earlier, so
nothing has to guess what another walker holds in flight. A cold run that
finds no line is put back as due now and tries again next pass.

This module answers two questions for that:

  lines_for_template   which number a template dials through, every
                       template on that number, and its lines — the
                       template's own pinned number, nothing else (ruled 22
                       Sep 2026: no shared pool, no summing); cached
                       CRM_CHANNEL_CACHE_SECONDS. None = no number pinned.
  claim_cold_runs      the coarse gate before a plan's cold runs are
                       claimed: nothing while its numbers report no free
                       line (so a full dialler costs no churn), else up to
                       what looks free — the insert is the exact gate. A
                       plan with no call square needs no line and is
                       claimed cold freely; a plan whose call template has
                       no number pinned is never claimed cold, and says so.

Nothing here reads Redis or the dialler's semaphore; capacity is the
number's own column, the rest is our tables.
"""

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.core.config.static import CRM_CHANNEL_CACHE_SECONDS, CRM_WALKER_LEASE_SECONDS
from app.core.logger import logger
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import EnrollmentRun
from app.database.accessor import (
    count_calls_holding_lines,
    get_telephony_number_by_id,
    get_template_by_id,
    get_template_ids_by_telephony_number,
)
from app.schemas.breeze_buddy.core import TelephonyNumberStatus

LOG_COMPONENT = "crm.outreach.capacity"


@dataclass(frozen=True)
class Lines:
    """One number's lines: its id, every template that dials through it,
    and how many calls it can hold at once."""

    number_id: str
    template_ids: Tuple[str, ...]
    capacity: int


_LINES_CACHE: Dict[str, Tuple[float, Optional[Lines]]] = {}
_TEMPLATES_CACHE: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}


async def lines_for_template(template_id: str) -> Optional[Lines]:
    """The number this template dials through, as the dialler will use it:
    the template's own pinned, AVAILABLE number. None when it has none —
    a plan that cannot size its lines. NULL lines read as one, as the
    dialler's reconciler reads them; 0 is a number with no lines."""
    cached = _LINES_CACHE.get(template_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    lines = await _resolve_lines(template_id)
    _LINES_CACHE[template_id] = (now + CRM_CHANNEL_CACHE_SECONDS, lines)
    return lines


async def _resolve_lines(template_id: str) -> Optional[Lines]:
    template = await get_template_by_id(template_id)
    if template is None or not template.telephony_number_id:
        return None
    number = await get_telephony_number_by_id(str(template.telephony_number_id))
    if number is None or number.status != TelephonyNumberStatus.AVAILABLE:
        return None
    siblings = await get_template_ids_by_telephony_number(number.id)
    if siblings is None:
        return None  # blind: size nothing rather than something
    capacity = 1 if number.maximum_channels is None else number.maximum_channels
    return Lines(number.id, tuple(sorted({template_id, *siblings})), capacity)


async def plan_call_templates(merchant_id: str, workflow_id: str) -> List[str]:
    """The call templates of the plan's LIVE document, cached with the
    lines: which numbers a plan dials through is an operating fact of
    today's dialler, not of the version a run entered under."""
    key = (merchant_id, workflow_id)
    cached = _TEMPLATES_CACHE.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    workflow = await workflow_accessor.get_workflow(merchant_id, workflow_id)
    definition = workflow.definition if workflow else None
    nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    templates: List[str] = []
    for node in nodes:
        if (
            isinstance(node, dict)
            and node.get("type") == "call"
            and node.get("template_id")
        ):
            template_id = str(node["template_id"])
            if template_id not in templates:
                templates.append(template_id)
    _TEMPLATES_CACHE[key] = (now + CRM_CHANNEL_CACHE_SECONDS, templates)
    return templates


async def free_lines(merchant_id: str, workflow_id: str) -> Optional[int]:
    """How many lines the plan's numbers have free right now, summed; None
    when the plan has no call square (nothing to gate) or a count could
    not be read (claim nothing cold: waiting a second is safe, flooding is
    not). A call template with no number pinned makes the plan unsizable:
    logged, 0."""
    templates = await plan_call_templates(merchant_id, workflow_id)
    if not templates:
        return None
    log = logger.bind(
        component=LOG_COMPONENT, merchant_id=merchant_id, workflow_id=workflow_id
    )
    numbers: Dict[str, Lines] = {}
    for template_id in templates:
        lines = await lines_for_template(template_id)
        if lines is None:
            log.warning(
                f"plan {workflow_id}: call template {template_id} has no telephony "
                f"number pinned — its cold runs are not claimed"
            )
            return 0
        numbers[lines.number_id] = lines
    free = 0
    for lines in numbers.values():
        holding = await count_calls_holding_lines(list(lines.template_ids))
        if holding is None:
            log.warning("lines unreadable — claiming no cold run this pass")
            return 0
        free += max(0, lines.capacity - holding)
    return free


async def claim_cold_runs(
    merchant_id: str, workflow_id: str, room_left: int
) -> List[EnrollmentRun]:
    """The walker's cold claim for one plan: oldest held first, as many as
    the batch has left — but none while the plan's numbers report no free
    line. The insert at the call square is the exact gate; this one only
    keeps a full dialler from costing a claim-and-release every pass."""
    if room_left <= 0:
        return []
    free = await free_lines(merchant_id, workflow_id)
    limit = room_left if free is None else min(room_left, free)
    if limit <= 0:
        return []
    return await enrollment_accessor.claim_due_runs(
        limit,
        CRM_WALKER_LEASE_SECONDS,
        lane="cold",
        merchant_id=merchant_id,
        workflow_id=workflow_id,
    )


def forget_lines() -> None:
    """Drop the caches — tests, and a future admin route."""
    _LINES_CACHE.clear()
    _TEMPLATES_CACHE.clear()
