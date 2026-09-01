"""Who hears an attributed event — the slot record OWNS but never fills.

Record decides WHEN consumers run (per row, inside the row's savepoint,
before its stamp — a poison consumer costs one row per poll, never the
batch). It must not know WHO runs: a subscriber import here points the
arrow record -> subscriber while every subscriber already reads record's
contracts, which is the import cycle the #1029 round hit. So the WHO lives
with worker_main — the one leaf allowed to see every module side by side —
which registers subscribers at import through this file. The boundary
checker (rule 12) fails CI on any record import of a subscriber module, so
the inversion cannot quietly regress.

A LIST, not a dict: consumers carry no names to collide on, and
registration order is execution order. Entry rules are the first entry;
segments and the transactional-send consumer (A13) join as one
``register_consumer`` line in worker_main each — zero edits in the pass.
"""

from typing import Awaitable, Callable, Dict, List, Optional

from app.crm.record.schemas import RawEvent

# One attributed event in: (event, customer_id, handles). ``handles`` is
# what the source's extractor found — a consumer never re-hunts the
# payload (two searches drift; the parked-Shopify-run scar is the proof).
Consumer = Callable[[RawEvent, str, Optional[Dict[str, str]]], Awaitable[None]]

_CONSUMERS: List[Consumer] = []


def register_consumer(consumer: Consumer) -> None:
    """Idempotent: worker imports can run more than once (tests, reload);
    the same function registering again must be a no-op, never a second
    delivery of every event."""
    if consumer not in _CONSUMERS:
        _CONSUMERS.append(consumer)


def consumers() -> List[Consumer]:
    """A copy, so the pass iterates a stable list and no caller can mutate
    the registry through the return value."""
    return list(_CONSUMERS)
