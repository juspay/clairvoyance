"""Work a worker pass must not do inside its transaction.

A pass holds one transaction for its whole batch (its claim's row locks live
exactly as long as it does). A call to another system made from inside it, a
Redis write for instance, would hold those locks and that connection for as
long as the other system takes. Such work is deferred instead: a pass opens
``collecting()`` around its transaction, the code that would call out hands
the call to ``defer()``, and the pass runs the calls after its commit.

Outside a collecting pass ``defer`` answers False and the caller runs the call
itself, as before.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator, List, Optional

from app.core.logger import logger

Job = Callable[[], Awaitable[None]]

_PENDING: ContextVar[Optional[List[Job]]] = ContextVar("crm_after_commit", default=None)


@contextmanager
def collecting() -> Iterator[List[Job]]:
    """The calls deferred while the block runs (the pass's transaction)."""
    jobs: List[Job] = []
    token = _PENDING.set(jobs)
    try:
        yield jobs
    finally:
        _PENDING.reset(token)


def defer(job: Job) -> bool:
    """Run ``job`` after the current pass commits. False when no pass is
    collecting: the caller runs it now."""
    pending = _PENDING.get()
    if pending is None:
        return False
    pending.append(job)
    return True


async def run(jobs: List[Job]) -> None:
    """Each job once, in order; one failing never stops the rest."""
    for job in jobs:
        try:
            await job()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"after-commit job failed: {type(e).__name__}: {e}")
