"""Repeat entries — debounce and refresh (modules/05-outreach §Repeat entries,
sealed 31 Aug 2026). Humans act in sessions; event streams arrive in bursts.
When a second entry event arrives for a run that is still standing on its
first square, the open-run unique refuses a second run (dedupe, built) — and
this file decides what the FIRST run does with the repeat, per the plan's
own words:

  on_repeat        ignore (default — today's behaviour) · refresh_latest ·
                   refresh_max(<payload field>) · accumulate
  debounce_minutes every matching repeat slides the entry wait's alarm

Priya abandons six carts between 10:00 and 10:09; the plan says "message 10
minutes after abandonment". Without this, the run born at 10:00 holds cart
#1 (Rs 800) and fires at 10:10 while she is still shopping. With
refresh_max(cart_value) + debounce_minutes 10, each repeat patches the
unfired run — context keeps the Rs 4,500 cart, the alarm slides to now+10 —
and at 10:19 she gets ONE message, about the biggest cart, after she has
actually gone quiet.

Mechanics: gather (the repeat's small facts) -> decide (PURE: what to write)
-> apply (ONE idempotent UPDATE, the reply's shape):
`WHERE status='waiting' AND current_node = <entry node>` — a run past its
first square is NEVER patched; what it already said was true when it said
it. Each event marks itself used (context.repeat_event_ids), so the spine's
at-least-once redelivery cannot slide the alarm twice for one event. The
row is found by enrollment_key, not customer_id: with entry.key an order
edit must patch ITS order's run, never a sibling's. The words judged are
the OPEN RUN'S version's (phase 13): the consumer hands over the pinned
definition of the run it found, so a v3 run keeps v3's on_repeat and its
first square's id even after v5 renamed both.

Plan vocabulary, never walker behaviour: WISMO (keyed) and transactional
flows leave on_repeat at ignore and debounce at 0.
"""

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger
from app.crm.outreach.db import accessor
from app.crm.outreach.schemas import WorkflowEntry, WorkflowEntryAt

# The words, exactly as the corpus writes them. refresh_max carries its
# field in parentheses: refresh_max(cart_value).
POLICY_IGNORE = "ignore"
POLICY_REFRESH_LATEST = "refresh_latest"
POLICY_REFRESH_MAX = "refresh_max"
POLICY_ACCUMULATE = "accumulate"
REPEAT_POLICIES = (
    POLICY_IGNORE,
    POLICY_REFRESH_LATEST,
    POLICY_REFRESH_MAX,
    POLICY_ACCUMULATE,
)
_REFRESH_MAX = re.compile(r"^refresh_max\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$")

# Where the repeats live in a run's context. repeat_event_ids and
# repeat_items are walker bookkeeping (nodes.run_facts drops them — a list
# can never be a template variable); repeat_count is a fact a template may
# read ("{repeat_count} orders").
REPEAT_EVENTS_KEY = "repeat_event_ids"
REPEATS_KEY = "repeat_items"
REPEAT_COUNT_KEY = "repeat_count"


def parse_repeat_policy(word: str) -> Optional[Tuple[str, Optional[str]]]:
    """PURE: the document's word -> (policy, field) or None when it is not
    in the vocabulary. refresh_max needs its field; the others carry none."""
    if word in (POLICY_IGNORE, POLICY_REFRESH_LATEST, POLICY_ACCUMULATE):
        return word, None
    match = _REFRESH_MAX.match(word or "")
    if match:
        return POLICY_REFRESH_MAX, match.group(1)
    return None


@dataclass(frozen=True)
class RepeatPlan:
    """What the one UPDATE will write. patch merges into context (or is
    appended under repeat_items when accumulate); max_field/max_value make
    the merge conditional on the new value winning; debounce_minutes > 0
    slides the alarm."""

    patch: Dict[str, Any]
    accumulate: bool
    max_field: Optional[str]
    max_value: Optional[float]
    debounce_minutes: float

    @property
    def is_noop(self) -> bool:
        return not self.patch and not self.accumulate and self.debounce_minutes <= 0


def _as_number(value: Any) -> Optional[float]:
    """PURE: a payload value as a FINITE float, else None. "nan"/"inf" are
    refused on purpose — Postgres orders NaN above every number, so a junk
    value would always win refresh_max (N18 from the #1041 review)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def repeat_plan(door: WorkflowEntry, facts: Dict[str, Any]) -> RepeatPlan:
    """PURE decide: given the door's entry words and the repeat's small
    facts, what does the open run get?

      ignore          -> nothing in context (the alarm may still slide)
      refresh_latest  -> the newest facts win, always
      refresh_max(f)  -> the newest facts win only if facts[f] beats the
                         run's f (compared in the statement; a non-numeric
                         new value never wins)
      accumulate      -> the facts are appended under repeat_items
    """
    parsed = parse_repeat_policy(door.on_repeat)
    policy, field = parsed if parsed else (POLICY_IGNORE, None)
    debounce = float(door.debounce_minutes or 0)
    if policy == POLICY_REFRESH_LATEST:
        return RepeatPlan(dict(facts), False, None, None, debounce)
    if policy == POLICY_REFRESH_MAX:
        value = _as_number(facts.get(field or ""))
        if value is None:
            return RepeatPlan({}, False, None, None, debounce)
        return RepeatPlan(dict(facts), False, field, value, debounce)
    if policy == POLICY_ACCUMULATE:
        return RepeatPlan(dict(facts), True, None, None, debounce)
    return RepeatPlan({}, False, None, None, debounce)


async def apply_repeat(
    merchant_id: str,
    workflow_id: str,
    enrollment_key: str,
    door: WorkflowEntryAt,
    event_id: str,
    facts: Dict[str, Any],
) -> bool:
    """A refused enrol may be a repeat of an open run standing on the
    door's start square. Returns True when a run was patched. Zero rows
    is the normal answer for "not a repeat" (nothing open, or the run
    already moved on) and for a redelivered event (it marked itself used
    the first time)."""
    plan = repeat_plan(door, facts)
    if plan.is_noop:
        return False  # ignore + no debounce: exactly today's behaviour
    patched = await accessor.patch_open_run(
        merchant_id,
        workflow_id,
        enrollment_key,
        door.start,
        event_id,
        plan.patch,
        plan.accumulate,
        plan.max_field,
        plan.max_value,
        plan.debounce_minutes,
    )
    if patched:
        logger.info(
            f"repeat entry applied: workflow {workflow_id} key {enrollment_key} "
            f"policy {door.on_repeat} debounce {plan.debounce_minutes}m"
        )
    return patched
