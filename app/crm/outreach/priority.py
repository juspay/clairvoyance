"""A call's rank, from the plan's `priority` block (schemas.WorkflowPriority). PURE.

The plan decides how urgent a call is; the dialler only compares the numbers it
is handed. A rule may read three facts about the run, answered only here:
run.latest_topic and run.latest_event_at (the stamps entry.py writes with every
producer letter, on plans that declare `priority`), and run.latest_event_today.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.crm.outreach.nodes.context import LATEST_EVENT_AT_KEY, LATEST_TOPIC_KEY
from app.crm.outreach.schemas import WaitWindow, WorkflowDefinition
from app.crm.outreach.window import _clock
from app.crm.shared.predicate import matches

# The facts a rule may read, in the order rank_for answers them.
FACTS = ("run.latest_topic", "run.latest_event_at", "run.latest_event_today")
# The order inside a rank the plan does not list.
DEFAULT_ORDER = "newest_event"


def order_of(ranks: Dict[str, Any], rank: Any) -> str:
    """PURE: the order inside `rank`: what the plan lists, else DEFAULT_ORDER."""
    return ranks.get(str(rank), DEFAULT_ORDER)


def in_todays_window(at: datetime, window: WaitWindow, now: datetime) -> bool:
    """PURE: is `at` inside the calling window that opens on `now`'s date, on
    the window's own clock? `closes` is exclusive, so 09:30 is not inside a
    10:00 window. A window across midnight is the night `now` is in."""
    zone = ZoneInfo(window.timezone)
    local = now.astimezone(zone)
    start, end = _clock(window.opens), _clock(window.closes)
    overnight = end < start
    day = local.date()
    if overnight and local.time() < start:
        day -= timedelta(days=1)
    opens = datetime.combine(day, start, tzinfo=zone)
    closes = datetime.combine(day + timedelta(days=int(overnight)), end, tzinfo=zone)
    return opens <= at < closes


def rank_for(
    definition: WorkflowDefinition, context: Dict[str, Any], now: datetime
) -> Optional[Dict[str, Any]]:
    """PURE: this run's call rank judged at `now`, in the shape the lead
    carries it (meta_data.priority): {rank, order, event_ms}. None when the
    plan declares no `priority`. The first rule that holds names the rank,
    none names `else`. A run older than the stamps is read from its founding
    letter's time; with no time at all `today` holds for nobody."""
    block = definition.priority
    if block is None:
        return None
    raw = context.get(LATEST_EVENT_AT_KEY) or context.get("entered_event_at")
    event_at = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    today = in_todays_window(event_at, block.window, now) if event_at else None
    facts = dict(zip(FACTS, (context.get(LATEST_TOPIC_KEY), raw, today)))
    rank = next(
        (rule.rank for rule in block.rules if matches(rule.if_, facts.get)),
        block.else_,
    )
    return {
        "rank": rank,
        "order": order_of(block.ranks, rank),
        "event_ms": int(event_at.timestamp() * 1000) if event_at else 0,
    }


def laws(definition: WorkflowDefinition) -> List[str]:
    """PURE: why this plan's `priority` may not be published (empty = fine).
    A rule may read only FACTS: any other field never holds, and every call
    would silently take `else`."""
    block = definition.priority
    if block is None:
        return []
    return [
        f"priority: a rule reads {c.field!r}, not one of {', '.join(FACTS)}"
        for rule in block.rules
        for c in rule.if_
        if c.field not in FACTS
    ]
