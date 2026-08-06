"""
Phase timing instrumentation.

Emits one structured line per instrumented step:

    [PHASE] resolve_call_templates elapsed_ms=8.4 call_id=abc123

Why this exists: during the 27 Jul incident, per-call latency had to be
reconstructed by matching timestamps across interleaved log lines from
hundreds of concurrent calls. That produced two wrong root causes before
real data arrived ("the policy loop is 98% of the latency" — measured at
2.8%; "the DB pool is too small" — it wasn't). Attributing time directly
to a named step removes the guesswork.

Uses ``time.perf_counter`` (monotonic, unaffected by wall-clock changes).

Cost (measured, not estimated)
------------------------------
``timed_phase`` costs **~35 us** per invocation against an ``enqueue=True``
sink, which is how every sink in ``app/core/logger`` is configured. The
inbound answer path emits **12 phase lines per call** (9 fixed + one
``policy_template`` per template; 3 templates on the Namma Yatri number):

    12 phases x 35 us = ~0.42 ms of event-loop CPU per call
      10 calls/sec -> 4.2 ms/sec  = 0.42% of one core
      25 calls/sec -> 10.6 ms/sec = 1.06% of one core
      50 calls/sec -> 21.1 ms/sec = 2.11% of one core

Backpressure: because every sink sets ``enqueue=True``, the formatting and
write happen on loguru's own writer thread, so a slow stdout or log driver
does NOT block the event loop. The trade-off is that loguru's queue is
unbounded — sustained writer lag shows up as memory growth, not as latency.
At 12 extra lines per call that headroom is large, but it is the thing to
watch if log volume is ever increased further.


Note: the emitted line inherits whatever ``set_log_context`` put in scope,
so ``call_id`` appears automatically on the JSON sink without being passed
as a field here.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.core.logger import logger


@contextmanager
def timed_phase(phase: str, **fields: Any) -> Iterator[None]:
    """
    Time the wrapped block and log ``[PHASE] <phase> elapsed_ms=<n>``.

    The line is emitted in a ``finally``, so a block that raises is still
    measured — that is usually the case you most want timing for.

    Args:
        phase: Step name. Keep it stable — log analysis aggregates by this
            string, so renaming one breaks historical comparisons.
        **fields: Extra ``key=value`` pairs appended to the line, in order.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        extras = "".join(f" {key}={value}" for key, value in fields.items())
        logger.info(f"[PHASE] {phase} elapsed_ms={elapsed_ms:.1f}{extras}")
