"""Write-behind metrics: defer the stats-only HIT writes off the read path.

A cache HIT currently awaits a SQLite ``touch_and_record`` (a WAL commit) before
returning audio. This batches those updates — per-key touch sums + summed metric
deltas — and flushes them on an interval, a batch threshold, or shutdown, so a HIT
returns audio without waiting on the DB writer.

Stats-only: a crash can lose at most one flush-window of *counters* (never wrong
audio — the cache row itself is unchanged, and the correctness-critical writes
``put`` / ``put_with_totals`` / ``delete`` / ``adjust_totals`` stay synchronous on
the metadata store).
"""

from __future__ import annotations

import asyncio

from app.core.logging import logger


class WriteBehindMetrics:
    """Batch + defer ``touch_and_record`` and ``record_metrics`` behind the read path."""

    def __init__(self, metadata, flush_interval_s: float, flush_batch: int):
        self._meta = metadata
        self._flush_interval = flush_interval_s
        self._flush_batch = max(1, flush_batch)
        self._touches: dict[str, dict[str, int]] = {}
        self._counters: dict[str, int] = {}
        # Per-provider accumulators: provider-given calls route here (flushed via
        # record_metrics(provider=...) which writes global + per-provider in one
        # store call, so the global daily row is never double-counted).
        self._provider_counters: dict[str, dict[str, int]] = {}
        self._touch_provider: dict[str, str] = {}  # key -> provider (constant per key)
        self._latency: list[tuple] = []  # (kind, latency_us) samples
        self._lock = asyncio.Lock()
        self._pending = 0
        self._task: asyncio.Task | None = None
        self._stopped = False
        # Strong refs to threshold-spawned flush tasks so the GC can't collect
        # them mid-flight (an unreferenced task may be cancelled before it runs).
        self._flush_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is None and not self._stopped:
            self._task = asyncio.create_task(self._run())

    def _spawn_flush(self) -> None:
        """Schedule a flush now (batch threshold crossed), keeping a strong ref."""
        task = asyncio.create_task(self._flush())
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Let any threshold-spawned flush finish writing its already-swapped-out
        # batch before draining — cancelling it mid-write would drop those
        # counters. Bounded by a timeout so a stuck flush can't hang shutdown;
        # anything still pending after is caught by the final drain below.
        if self._flush_tasks:
            _done, pending = await asyncio.wait(list(self._flush_tasks), timeout=5.0)
            for t in pending:
                t.cancel()
            self._flush_tasks.clear()
        await self._flush()  # final drain so graceful shutdown loses nothing

    async def touch_and_record(
        self, key: str, deltas: dict, *, provider: str | None = None
    ) -> None:
        async with self._lock:
            d = self._touches.setdefault(key, {})
            for k, v in deltas.items():
                d[k] = d.get(k, 0) + int(v)
            if provider is not None:
                self._touch_provider[key] = provider
            self._pending += 1
            hot = self._pending >= self._flush_batch
        if hot:
            self._spawn_flush()  # batch threshold -> flush promptly

    async def record_metrics(
        self, *, provider: str | None = None, **deltas: int
    ) -> None:
        async with self._lock:
            if provider is not None:
                pc = self._provider_counters.setdefault(provider, {})
                for k, v in deltas.items():
                    pc[k] = pc.get(k, 0) + int(v)
            else:
                for k, v in deltas.items():
                    self._counters[k] = self._counters.get(k, 0) + int(v)
            self._pending += 1
            hot = self._pending >= self._flush_batch
        if hot:
            self._spawn_flush()

    async def record_latency(
        self, kind: str, latency_us: int, provider: str | None = None
    ) -> None:
        async with self._lock:
            if len(self._latency) < 10000:  # bound memory if a flush ever stalls
                self._latency.append((kind, int(latency_us), provider))
            self._pending += 1
            hot = self._pending >= self._flush_batch
        if hot:
            self._spawn_flush()

    async def _flush(self) -> None:
        # Swap the accumulators out under the lock, then write without holding it
        # (so a slow flush doesn't block accumulation). Concurrent flushes are safe
        # — the later one swaps empty dicts and no-ops.
        async with self._lock:
            if not (
                self._touches
                or self._counters
                or self._provider_counters
                or self._latency
            ):
                self._pending = 0
                return
            touches, self._touches = self._touches, {}
            touch_provider, self._touch_provider = self._touch_provider, {}
            counters, self._counters = self._counters, {}
            provider_counters, self._provider_counters = self._provider_counters, {}
            latency, self._latency = self._latency, []
            self._pending = 0
        for key, d in touches.items():
            try:
                # The store's touch_and_record bumps hit_count by the ``hits``
                # delta (default 1) and sums all deltas into the daily rollup, so
                # one batched call == N synchronous calls exactly. The provider
                # (constant per key) is forwarded so the per-provider row is written.
                await self._meta.touch_and_record(
                    key, d, provider=touch_provider.get(key)
                )
            except Exception as e:
                logger.warning(f"write-behind touch flush failed ({key[:8]}…): {e}")
        if counters:
            try:
                await self._meta.record_metrics(**counters)
            except Exception as e:
                logger.warning(f"write-behind metrics flush failed: {e}")
        for prov, d in provider_counters.items():
            try:
                # record_metrics(provider=...) writes the global daily row AND the
                # per-provider subset in one store call (no double-count).
                await self._meta.record_metrics(provider=prov, **d)
            except Exception as e:
                logger.warning(
                    f"write-behind provider metrics flush failed ({prov}): {e}"
                )
        if latency:
            try:
                await self._meta.record_latency_batch(latency)
            except Exception as e:
                logger.warning(f"write-behind latency flush failed: {e}")

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"write-behind flush loop error: {e}")
                await asyncio.sleep(self._flush_interval)
