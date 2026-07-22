"""Per-provider resilience: bulkhead (concurrency cap) + token-bucket rate limit.

A **bulkhead** caps how many in-flight synth/stream calls one provider may have, so
a slow or hung provider can't exhaust shared resources (the single event loop, the
``to_thread`` worker pool, memory) and starve the *other* providers. The **rate
limiter** keeps us under provider concurrency/character limits (avoids 429s).

Both are configurable per provider via global defaults plus a JSON override (see
:mod:`app.core.config`). ``get_gate(provider)`` always returns a (cached) gate — a
no-op when both limiters are disabled (0) — so call sites just ``async with gate:``.
"""

from __future__ import annotations

import asyncio
import time

from app.core.config import settings


class ProviderBusy(Exception):
    """Bulkhead full — provider is at its concurrent-synth cap within the wait
    window. Maps to a retryable 503 (with ``Retry-After``)."""


class _TokenBucket:
    """Async token-bucket: ``rate`` tokens/sec, ``capacity`` burst. ``acquire`` waits."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity if capacity and capacity > 0 else rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self, n: float = 1.0) -> None:
        # Re-check under the lock, sleep for exactly the refill time needed, retry.
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self._rate if self._rate > 0 else 0.0
            await asyncio.sleep(wait)


class ResilienceGate:
    """Bulkhead (semaphore) + rate limiter (token bucket) for one provider.

    No-op when both ``max_concurrent`` and ``rate_per_sec`` are 0/None (the gate is
    still held/entered, it just does nothing) so call sites are unconditional.
    """

    def __init__(self, max_concurrent: int, rate_per_sec: float, wait_timeout: float):
        self._sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent) if max_concurrent and max_concurrent > 0 else None
        )
        self._bucket: _TokenBucket | None = (
            _TokenBucket(rate_per_sec) if rate_per_sec and rate_per_sec > 0 else None
        )
        self._wait_timeout = wait_timeout

    async def acquire(self) -> None:
        # Acquire the bulkhead first. If the rate-limiter then fails or is
        # cancelled, release the bulkhead ourselves so a partial acquire can't
        # leak a slot. On success, __aexit__ releases exactly once.
        if self._sem is not None:
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=self._wait_timeout)
            except asyncio.TimeoutError as e:
                raise ProviderBusy("provider at concurrent-synth cap") from e
        if self._bucket is not None:
            try:
                await self._bucket.acquire()
            except BaseException:
                if self._sem is not None:
                    self._sem.release()
                raise

    def release(self) -> None:
        # Paired 1:1 with a successful acquire() — every __aenter__ that returned
        # acquired exactly one bulkhead permit, so __aexit__ releases exactly one.
        # Deliberately NO per-call flag: the gate is a cached singleton shared by
        # ALL concurrent callers for a provider, so instance state would be
        # clobbered by overlapping holders and silently leak the semaphore
        # (under-releasing until the provider locks up with ProviderBusy).
        if self._sem is not None:
            self._sem.release()

    async def __aenter__(self) -> "ResilienceGate":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()


_GATE_CACHE: dict[str, ResilienceGate] = {}


def _config(provider: str) -> tuple[int, float, float]:
    overrides = settings.provider_resilience_overrides.get(provider, {}) or {}
    max_concurrent = int(overrides.get("max_concurrent", settings.provider_max_concurrent_synths))
    rate = float(overrides.get("rate_per_sec", settings.provider_rate_limit_per_sec))
    wait_ms = int(overrides.get("wait_timeout_ms", settings.provider_bulkhead_wait_timeout_ms))
    return max_concurrent, rate, wait_ms / 1000.0


def get_gate(provider: str) -> ResilienceGate:
    """Return the cached ResilienceGate for ``provider`` (no-op if disabled)."""
    gate = _GATE_CACHE.get(provider)
    if gate is None:
        mc, rate, wait = _config(provider)
        gate = ResilienceGate(mc, rate, wait)
        _GATE_CACHE[provider] = gate
    return gate


def reset_gates() -> None:
    """Drop cached gates (tests / config reload)."""
    _GATE_CACHE.clear()
