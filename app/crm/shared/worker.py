"""The shared drain-loop scaffold: poll cadence, jittered backoff, per-row
error isolation, heartbeat, log-context hygiene, graceful shutdown. It never
learns what a row means — the claim query and handling logic stay in each
owning module."""

import asyncio
import random
import time
from typing import Awaitable, Callable, List, TypeVar

from app.core.config.static import CRM_WORKER_HEARTBEAT
from app.core.logger import logger
from app.core.logger.context import clear_log_context

T = TypeVar("T")


async def run_drain_loop(
    claim: Callable[[int], Awaitable[List[T]]],
    handle: Callable[[T], Awaitable[None]],
    *,
    interval: float,
    batch: int,
    stop_event: asyncio.Event,
    name: str,
    concurrency: int = 1,
) -> None:
    """``claim`` returns the rows this iteration found (a txn-style claim has
    already committed them); ``handle`` is a per-row post-commit hook.

    The log context is cleared per iteration (the heartbeat fires BEFORE
    the claim that would reset it) and again per row — a handler runs in
    THIS task, so its ids outlive it. Same bug at two scales: a line filed
    under the wrong row.

    ``concurrency`` is how many of the CLAIMED rows are worked at once, and
    it belongs to the ROLE, not the scaffold — hence the default of one.
    The walker asks for more because its cost is WAITING: the playbook's
    llm_call is a model round trip per lead (p50 250ms, measured), so a
    serial batch is one wait after another and the pod idles at 3% CPU
    while a morning pile drains. The rows were claimed together and every
    walker write is lease-conditional, so working them together changes
    nothing but the waiting. The other two roles must NOT raise it: the
    dispatcher's bound is batch x 2 x send timeout < lease and assumes
    serial sends, and the event worker's claim carries a transaction whose
    connection is not shareable across tasks."""
    backoff = interval
    since_beat = 0
    last_beat = time.monotonic()
    while not stop_event.is_set():
        # The handler ran in THIS task, so its row's ids are still
        # standing — and the heartbeat below fires BEFORE the claim that
        # would reset them.
        clear_log_context()
        now = time.monotonic()
        # A silent worker and a dead one look identical in logs; this beat
        # is what tells them apart, so it fires on an idle loop too.
        if now - last_beat >= CRM_WORKER_HEARTBEAT:
            # worker as a field, so the absence rule that catches a hung
            # loop is a column lookup, not a scan of the whole stream.
            logger.bind(worker=name, rows_since_beat=since_beat).info(
                f"{name}: alive, {since_beat} rows since last heartbeat"
            )
            since_beat = 0
            last_beat = now

        try:
            rows = await claim(batch)
        except Exception as e:
            logger.bind(worker=name).error(f"{name}: claim failed: {e}")
            await _jittered_wait(backoff, stop_event)
            backoff = min(backoff * 2, 5.0)
            continue

        if not rows:
            await _jittered_wait(backoff, stop_event)
            backoff = min(backoff * 2, 5.0)
            continue

        backoff = interval
        # One gate per pass: asyncio.gather runs each row in its OWN task, so
        # the log context (a ContextVar) is copied per row and a handler's ids
        # can no longer outlive it into a sibling — the per-row clear below
        # stays, because a task inherits whatever stood at creation.
        gate = asyncio.Semaphore(max(1, concurrency))

        async def _work(row: T) -> None:
            nonlocal since_beat
            async with gate:
                if stop_event.is_set():
                    return
                since_beat += 1
                # A handler that raises before stamping its ids would report
                # the previous row's — worse than none, because it reads true.
                clear_log_context()
                try:
                    await handle(row)
                except Exception as e:
                    # Not cleared AFTER: what the handler stamped names the
                    # row that failed. Empty is honest; wrong is not.
                    logger.bind(worker=name).error(f"{name}: row failed, skipping: {e}")

        # At concurrency 1 the gate admits one row at a time in order, which
        # is the serial loop this replaced. return_exceptions keeps a
        # BaseException from one row — in practice the CancelledError of
        # shutdown — from abandoning the rows still in flight; they are
        # awaited first, and the cancel is re-raised after.
        outcomes = await asyncio.gather(
            *(_work(row) for row in rows), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, Exception
            ):
                raise outcome
        # full batch -> loop again immediately (no sleep)


async def _jittered_wait(base: float, stop_event: asyncio.Event) -> None:
    """Sleep ``base`` ±20%, or wake early if we are shutting down. The
    jitter keeps replicas that started together from claiming in lockstep;
    waiting on the event is what stops shutdown costing a full backoff."""
    delay = max(0.0, base * (1 + random.uniform(-0.2, 0.2)))
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
