"""The call ceiling — how many calls one run may place in a day, and whether
it has (canon T19 ``exits.max_calls_per_day``, rollout phase 20).

Beside ``window.py`` and for the same reason: plan-level scheduling
arithmetic, pure, with more than one caller — the call square before it
dials, the ``run.max_calls_reached`` fact a plan routes on
(``predicates.RUN_FACTS``), and the publish law. One implementation, so "may
I dial?" and "should I route past the dialling?" cannot disagree.

Not in ``nodes/``: ``nodes/context.py`` owns what in a run's context is OURS
versus the producer's, and a ceiling predicate is not that question. Its one
claim on that file is ``CALLS_TODAY_KEY``, which a test pins.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

# The day-stamped ledger behind exits.max_calls_per_day:
# {"day": "2026-09-22", "n": 3}. BOOKKEEPING (nodes/context.py names it):
# run_facts drops it and entry.py refuses a producer who spells it, which a
# call ceiling needs — admitted as a scalar it would read as junk, count 0,
# and hand the run a fresh allowance on merchant input.
#
# Its own key, NOT the visit counters: those key the lead ids (uuid5
# run:node:visit) and must stay monotonic. Reset at midnight they would
# re-derive yesterday's id, the PK would absorb it as a lease retry, and the
# first call of the new day would silently never be placed (967a86df).
CALLS_TODAY_KEY = "calls_today"


def today_on(exits: Any, now: Optional[datetime] = None) -> str:
    """PURE: the plan's calendar day, as the call ledger stamps it.

    On the PLAN's clock, never the server's: 23:30 IST on the 22nd is still
    the 22nd, and a UTC day would have rolled it two hours earlier. UTC only
    for a plan with no clock, which is a plan with no ceiling to judge.
    """
    at = now or datetime.now(timezone.utc)
    zone = getattr(exits, "timezone", None)
    return (at.astimezone(ZoneInfo(zone)) if zone else at).date().isoformat()


def calls_today(context: Dict[str, Any], day: str) -> int:
    """PURE: calls this run has placed on ``day``, over every call square.

    A ledger stamped with any other day reads as 0 — that IS the midnight
    reset: no sweep, no cron, the first call of the new day re-stamps it.
    Junk reads as 0.
    """
    ledger = context.get(CALLS_TODAY_KEY)
    if not isinstance(ledger, dict) or ledger.get("day") != day:
        return 0
    placed = ledger.get("n")
    return placed if isinstance(placed, int) and placed >= 0 else 0


def max_calls_reached(context: Dict[str, Any], exits: Any) -> bool:
    """PURE: has this run spent today's call allowance?

    THE predicate, and the one a plan names as ``run.max_calls_reached``.
    Computed on every question, never stored: a stored answer goes stale the
    moment the day rolls, since the ledger un-caps itself by re-stamp.
    """
    ceiling = getattr(exits, "max_calls_per_day", None)
    if ceiling is None:
        return False
    return calls_today(context, today_on(exits)) >= ceiling
