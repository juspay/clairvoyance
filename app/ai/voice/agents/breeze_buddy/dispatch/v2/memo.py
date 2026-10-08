"""A per-pod memo for the v2 dial path's per-template reads (spec 2026-10-05 §4.10).

Every v2 dial read its template (the whole flow), its call config and its number from the
DB: 3 of the ~31 transactions per call, for rows that change rarely. A value is kept
``ttl_s`` (BB_V2_DIAL_MEMO_TTL_S; 0 turns the memo off), so an edit reaches every dial of
the template within that. Concurrent misses for one key share one load, and a cancelled
caller never cancels the load the others wait on. A None answer or an error is never
kept: a fixed template works at once. Entries older than ``ttl_s`` are dropped at most once
per ``ttl_s``, so memory follows the templates dialled lately. Values are shared between
dials: callers must not change them (``apply_playground_overrides`` copies first).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Tuple


class TTLMemo:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._clock = clock
        self._data: Dict[Hashable, Tuple[float, Any]] = {}
        self._loading: Dict[Hashable, asyncio.Task] = {}
        self._pruned = clock()

    async def get(self, key: Hashable, load: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._data.get(key)
        if hit is not None and self._clock() - hit[0] < self._ttl:
            return hit[1]
        task = self._loading.get(key)
        if task is None:
            task = asyncio.ensure_future(self._load(key, load))
            self._loading[key] = task
        return await asyncio.shield(task)

    def forget(self, key: Hashable) -> None:
        """Drop ``key``: its value turned out stale. A load already running for it still
        answers its own callers but is not kept, so the next ``get`` reads afresh."""
        self._data.pop(key, None)
        self._loading.pop(key, None)

    async def _load(self, key: Hashable, load: Callable[[], Awaitable[Any]]) -> Any:
        me = asyncio.current_task()
        kept = False
        try:
            value = await load()
        finally:
            kept = self._loading.get(key) is me  # not forgotten meanwhile
            if kept:
                del self._loading[key]
        if kept and value is not None:  # (ttl 0: each store drops the one before)
            now = self._clock()
            if now - self._pruned >= self._ttl:
                self._data = {
                    k: v for k, v in self._data.items() if now - v[0] < self._ttl
                }
                self._pruned = now
            self._data[key] = (now, value)
        return value
