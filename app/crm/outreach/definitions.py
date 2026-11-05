"""Which document a run executes (ADR 0023) — ONE answer for the two
readers that need it: the walker (rollout phase 12) and the entry
consumer's per-run pass (phase 13).

A run's pin — crm_workflow_enrollment.workflow_version — names the
crm_workflow_version row it executes. Rows there are immutable (064's
trigger refuses every UPDATE) and never deleted (ADR 0023 §5), so a
document read once is true for as long as the process lives: the cache
below never invalidates, it only evicts — least recently used first, past
the bound. A migrate publish re-pins open runs
by changing the run's version NUMBER, so the next read of that run lands
on a different key with nothing to invalidate.

Logic, not db: the only db-world import is the module's accessor door.
"""

import asyncio
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from app.crm.outreach.db.accessors import (
    version as version_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition

# Sized for one merchant fleet's live versions many times over; §14.7's
# cost of pinning is otherwise one indexed point read per claim or event.
_DEFINITION_CACHE_SIZE = 512
_definitions: OrderedDict[Tuple[str, int], WorkflowDefinition] = OrderedDict()


async def definition_for(run: EnrollmentRun) -> Optional[WorkflowDefinition]:
    """The document this run executes: its pin, by (workflow, version),
    from the cache or one indexed point read. None when no such version
    row exists — the caller says what that means (the walker parks the
    run honestly; the consumer leaves it for the walker), never a
    fallback to the live document, which would judge a run by a plan it
    did not enter under."""
    key = (str(run.workflow_id), run.workflow_version)
    cached = _definitions.get(key)
    if cached is not None:
        _definitions.move_to_end(key)
        return cached
    document = await version_accessor.get_definition(run.merchant_id, key[0], key[1])
    if document is None:
        return None
    definition = WorkflowDefinition.model_validate(document)
    _definitions[key] = definition
    while len(_definitions) > _DEFINITION_CACHE_SIZE:
        _definitions.popitem(last=False)
    return definition


# ---- the LIVE document, for entry's door match (a different question) ----
#
# definition_for above answers "what does this run execute" and is keyed by a
# run's pin. Entry asks "does any live door name this topic", which is about
# the CURRENT version of a live plan, and it does not need the playbook.
#
# Deliberately its OWN map, not a second writer into _definitions. The key
# shape is the same (workflow, version), but the documents are not: this one
# is playbook-stripped. Sharing the map would let whichever reader missed
# first decide what every later reader gets, and a walker node that found
# `definition.playbook is None` would fail OPEN — blocks_for returns ({}, {})
# and the call goes out with its words missing, silently. One map per
# projection is what makes that impossible rather than merely unlikely.
_LIVE_CACHE_SIZE = 256
_live_definitions: OrderedDict[Tuple[str, int], WorkflowDefinition] = OrderedDict()
_live_pending: Dict[Tuple[str, int], "asyncio.Future[Any]"] = {}


def reset_caches() -> None:
    """Drop both maps — for tests, which otherwise pass because a sibling
    warmed a key. Production never calls this: a version is immutable, so
    there is nothing to invalidate."""
    _definitions.clear()
    _live_definitions.clear()
    _live_pending.clear()


async def live_definition(
    merchant_id: str, workflow_id: str, version: int
) -> Optional[WorkflowDefinition]:
    """The live document entry judges a topic against, at the version the
    routing read named, minus its playbook.

    None when that version is no longer live — a publish landed between
    routing and here. The caller skips the plan for this one event; the
    next event routes to the new version, which is a new key.

    Single-flighted: the map is written only after an await, so without
    this the N rows of one batch that want the same plan would each miss
    and each read. Awaiting the shared future through a shield means one
    caller's cancellation cannot cancel the future every other caller is
    holding.
    """
    key = (workflow_id, version)
    cached = _live_definitions.get(key)
    if cached is not None:
        _live_definitions.move_to_end(key)
        return cached

    inflight = _live_pending.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    pending: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
    _live_pending[key] = pending
    try:
        document = await workflow_accessor.live_definition(
            merchant_id, workflow_id, version
        )
        definition = (
            WorkflowDefinition.model_validate(document)
            if document is not None
            else None
        )
    except BaseException:
        if not pending.done():
            pending.set_result(None)
        raise
    else:
        if definition is not None:
            _live_definitions[key] = definition
            while len(_live_definitions) > _LIVE_CACHE_SIZE:
                _live_definitions.popitem(last=False)
        if not pending.done():
            pending.set_result(definition)
        return definition
    finally:
        _live_pending.pop(key, None)
