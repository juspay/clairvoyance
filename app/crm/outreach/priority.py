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
from app.crm.shared.predicate import ORDER_OPS, PRESENCE_OPS, TEXT_OPS, matches

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


def _call_hours(config: Any) -> Optional[WaitWindow]:
    """PURE: a template's call hours (its call_execution_config) as a window,
    on the clock the dialler reads them by (IST, managers/calls.py)."""
    if config is None or config.call_start_time == config.call_end_time:
        return None
    return WaitWindow(
        opens=f"{config.call_start_time:%H:%M}",
        closes=f"{config.call_end_time:%H:%M}",
        timezone="Asia/Kolkata",
    )


def rank_for(
    definition: WorkflowDefinition,
    context: Dict[str, Any],
    now: datetime,
    config: Any = None,
) -> Optional[Dict[str, Any]]:
    """PURE: this run's call rank judged at `now`, in the shape the lead
    carries it (meta_data.priority): {rank, order, event_ms}. None when the
    plan declares no `priority`. The first rule that holds names the rank,
    none names `else`. A live call also carries next_rank / next_order.
    "Today" is the call hours of `config` (the call template's
    call_execution_config) unless the plan names its own window. A run older
    than the stamps is read from its founding letter's time; with no time or
    no hours at all `today` holds for nobody."""
    block = definition.priority
    if block is None:
        return None
    raw = context.get(LATEST_EVENT_AT_KEY) or context.get("entered_event_at")
    event_at = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    window = block.window or _call_hours(config)
    today = in_todays_window(event_at, window, now) if event_at and window else None
    facts = dict(zip(FACTS, (context.get(LATEST_TOPIC_KEY), raw, today)))
    # Judged twice: as it is, and as if the letter were not today's.
    rank, later = [
        next(
            (rule.rank for rule in block.rules if matches(rule.if_, seen.get)),
            block.else_,
        )
        for seen in (facts, {**facts, FACTS[2]: False})
    ]
    answer: Dict[str, Any] = {
        "rank": rank,
        "order": order_of(block.ranks, rank),
        "event_ms": int(event_at.timestamp() * 1000) if event_at else 0,
    }
    # A live call not placed by closing is pile tomorrow: what it falls to.
    if later != rank:
        answer.update(next_rank=later, next_order=order_of(block.ranks, later))
    return answer


def laws(definition: WorkflowDefinition) -> List[str]:
    """PURE: why this plan's `priority` may not be published (empty = fine).
    A rule may read only FACTS: any other field never holds, and every call
    would silently take `else`."""
    block = definition.priority
    if block is None:
        return []
    problems: List[str] = [
        f"priority: ranks key {key!r} is not a rank from 1 to 99"
        for key in block.ranks
        if not (key.isdigit() and str(int(key)) == key and 1 <= int(key) <= 99)
    ]
    # the topics this plan can stamp: its doors' and its listening squares'
    heard = {door.topic for door in definition.entries} | {
        topic for node in definition.nodes for topic in node.topics
    }
    for rule in block.rules:
        for c in rule.if_:
            if c.field not in FACTS:
                problems.append(
                    f"priority: a rule reads {c.field!r}, not one of {', '.join(FACTS)}"
                )
            elif c.op not in PRESENCE_OPS and not _fits(c.field, c.op, c.value):
                problems.append(
                    f"priority: {c.field} {c.op} {c.value!r} can never hold"
                )
            elif c.field == "run.latest_topic" and c.op not in PRESENCE_OPS:
                values = c.value if c.op == "in" else [c.value]
                problems += [
                    f"priority: a rule names {v!r}, but no square in this "
                    "workflow listens for it"
                    for v in values
                    if v not in heard
                ]
    return problems


def _fits(field: str, op: str, value: Any) -> bool:
    """PURE: can `field op value` ever hold? The topic is text (is, is_not,
    in), "today" a boolean (is, is_not), the event time a moment with its zone
    (the ordering ops). Anything else never holds, and every call would
    silently take `else`."""
    if field == "run.latest_topic":
        values = value if op == "in" else [value]
        return op in TEXT_OPS and all(isinstance(v, str) for v in values)
    if field == "run.latest_event_today":
        return op in ("is", "is_not") and isinstance(value, bool)
    if op not in ORDER_OPS or not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False
