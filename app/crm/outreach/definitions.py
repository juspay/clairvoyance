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

from collections import OrderedDict
from typing import Optional, Tuple

from app.crm.outreach.db import accessor
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
    document = await accessor.get_definition(run.merchant_id, key[0], key[1])
    if document is None:
        return None
    definition = WorkflowDefinition.model_validate(document)
    _definitions[key] = definition
    while len(_definitions) > _DEFINITION_CACHE_SIZE:
        _definitions.popitem(last=False)
    return definition
