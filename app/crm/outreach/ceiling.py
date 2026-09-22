"""The call ceiling — how many calls one run may place in a day, and whether
it has (canon T19 ``exits.max_calls_per_day``, rollout phase 20).

Beside ``window.py`` and for the same reason: a plan-level scheduling rule
whose arithmetic is pure, asked by more than one caller. Three ask it — the
call square before it dials, the ``run.max_calls_reached`` derived fact a
condition routes on (``predicates.RUN_FACTS``), and the publish law that
refuses a ceiling no square can reach. One implementation, so "may I dial?"
and "should I route past the dialling?" can never answer differently.

It lives here rather than in ``nodes/`` because it is not one word's private
business: ``nodes/context.py`` owns what in a run's context is OURS versus the
producer's, and a ceiling predicate is not that question. Its one claim on
that file is ``CALLS_TODAY_KEY``, which the bookkeeping list names and a test
pins.

Pure: no I/O, no clock but the one it is handed.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

# The day-stamped ledger behind exits.max_calls_per_day:
# {"day": "2026-09-22", "n": 3}. It is BOOKKEEPING (nodes/context.py names it
# in _BOOKKEEPING_KEYS): `run_facts` drops it so the raw dict never rides a
# lead payload, and entry.py refuses a producer who spells it — which a
# contact ceiling needs, since admitted as a scalar it would read as junk,
# count 0, and hand the run a fresh allowance on merchant input.
#
# It is its own key, NOT the visit counters: those key the lead ids
# (uuid5 run:node:visit) and must stay monotonic for the run's life.
# Resetting them at midnight would re-derive yesterday's id, the primary key
# would absorb it as a lease retry, and the first call of the new day would
# silently never be placed (the 967a86df scar, from the other side).
CALLS_TODAY_KEY = "calls_today"


def today_on(exits: Any, now: Optional[datetime] = None) -> str:
    """PURE: the plan's calendar day, as the call ledger stamps it.

    Read on the PLAN's clock (exits.timezone), never the server's: a run
    dialling at 23:30 IST is on the 22nd, and a UTC day would have rolled it
    to the 23rd two hours earlier. Falls back to UTC only for a plan with no
    clock, which is a plan with no ceiling to judge.
    """
    at = now or datetime.now(timezone.utc)
    zone = getattr(exits, "timezone", None)
    return (at.astimezone(ZoneInfo(zone)) if zone else at).date().isoformat()


def calls_today(context: Dict[str, Any], day: str) -> int:
    """PURE: calls this run has placed on ``day``, over every call square.

    A ledger stamped with any other day reads as 0 — that IS the midnight
    reset, and it needs no sweep, no cron and no write at the boundary: the
    first call of the new day simply re-stamps the record. Junk reads as 0.
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
    moment the day rolls — the ledger un-caps itself by re-stamp, a flag
    would need a write nobody makes until the next call.
    """
    ceiling = getattr(exits, "max_calls_per_day", None)
    if ceiling is None:
        return False
    return calls_today(context, today_on(exits)) >= ceiling
