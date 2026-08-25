"""
Shared fixtures for dispatch tests.

Most tests mock the Redis service rather than running real Redis. The goal
is to verify control flow, command shape, and guard logic — not Redis
semantics (which are Redis's contract, not ours).

End-to-end and chaos tests additionally use a `_DispatchHarness` fixture
that monkeypatches the worker's DB/provider collaborators so a full
``_iteration`` cycle can run without a real database or telephony backend.
"""

from __future__ import annotations

import asyncio
import fnmatch
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.services.redis as redis_pkg
from app.ai.voice.agents.breeze_buddy.dispatch import (
    alerts as alerts_mod,
    channel_semaphore as ch_mod,
    leader as leader_mod,
    promoter as prom_mod,
    queue as queue_mod,
    reconcilers as recon_mod,
    worker as worker_mod,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.schemas import CallProvider, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import (
    CallExecutionConfig,
    InboundBlockAction,
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)


class FakeRedisClient:
    """
    Tiny in-memory stand-in for the redis-py async client. Implements only
    the commands the dispatch module calls. Not a fakeredis replacement —
    intentionally simple so test failures point at logic, not at obscure
    redis-py edge cases.
    """

    def __init__(self) -> None:
        self.zsets: Dict[str, Dict[str, float]] = {}
        self.lists: Dict[str, List[str]] = {}
        self.kv: Dict[str, str] = {}
        self.expirations: Dict[str, int] = {}

    # -- ZSET ops -----------------------------------------------------------

    async def zadd(self, key: str, mapping: Dict[str, float]) -> int:
        z = self.zsets.setdefault(key, {})
        added = 0
        for m, s in mapping.items():
            if m not in z:
                added += 1
            z[m] = float(s)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        z = self.zsets.get(key, {})
        n = 0
        for m in members:
            if m in z:
                del z[m]
                n += 1
        return n

    async def zscore(self, key: str, member: str) -> Optional[float]:
        return self.zsets.get(key, {}).get(member)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zcount(self, key: str, mn, mx) -> int:
        """ZCOUNT min max — mn/mx may be "-inf"/"+inf" strings or numbers."""
        z = self.zsets.get(key, {})
        lo = float("-inf") if str(mn) == "-inf" else float(mn)
        hi = float("inf") if str(mx) == "+inf" else float(mx)
        return sum(1 for s in z.values() if lo <= s <= hi)

    async def zrangebyscore(
        self, key: str, mn: float, mx: float, start: int = 0, num: int = -1
    ) -> List[str]:
        z = self.zsets.get(key, {})
        items = sorted(((s, m) for m, s in z.items() if mn <= s <= mx))
        members = [m for _s, m in items]
        if num == -1:
            return members[start:]
        return members[start : start + num]

    # -- LIST ops -----------------------------------------------------------

    async def rpush(self, key: str, *vals: str) -> int:
        L = self.lists.setdefault(key, [])
        L.extend(vals)
        return len(L)

    async def lpush(self, key: str, *vals: str) -> int:
        L = self.lists.setdefault(key, [])
        for v in vals:
            L.insert(0, v)
        return len(L)

    async def lpop(self, key: str) -> Optional[str]:
        L = self.lists.get(key, [])
        return L.pop(0) if L else None

    async def lrem(self, key: str, count: int, value: str) -> int:
        """LREM with full Redis semantics:
          count > 0: remove up to count occurrences from head -> tail
          count < 0: remove up to abs(count) occurrences from tail -> head
          count = 0: remove all occurrences
        Returns the number of removed elements.
        """
        L = self.lists.get(key, [])
        if not L:
            return 0
        if count == 0:
            removed = L.count(value)
            self.lists[key] = [v for v in L if v != value]
            return removed
        if count > 0:
            indices = [i for i, v in enumerate(L) if v == value][:count]
        else:
            indices = [i for i, v in enumerate(L) if v == value][count:]
        for i in sorted(indices, reverse=True):
            del L[i]
        return len(indices)

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        L = self.lists.get(key, [])
        if end == -1:
            end = len(L)
        else:
            end = end + 1
        return L[start:end]

    async def blpop(self, key: str, timeout: int = 0) -> Optional[Tuple[str, str]]:
        L = self.lists.get(key, [])
        if L:
            return (key, L.pop(0))
        return None

    async def delete(self, key: str) -> int:
        n = 0
        for d in (self.zsets, self.lists, self.kv):
            if key in d:
                del d[key]
                n += 1
        return n

    async def exists(self, key: str) -> int:
        return int(key in self.kv or key in self.lists or key in self.zsets)

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 10
    ) -> Tuple[int, List[str]]:
        # Single-shot: return everything matching, signal end with cursor 0.
        keys = [k for k in self.lists.keys() if fnmatch.fnmatch(k, match)]
        return 0, keys

    def pipeline(self, transaction: bool = True):
        """Minimal pipeline mock — accumulates calls, executes on context exit."""
        outer = self

        class _Pipe:
            def __init__(self):
                self.ops: List[Tuple[str, tuple]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def delete(self, key: str):
                self.ops.append(("delete", (key,)))
                return self

            def rpush(self, key: str, *vals: str):
                self.ops.append(("rpush", (key, *vals)))
                return self

            async def execute(self) -> List[Any]:
                results = []
                for op, args in self.ops:
                    if op == "delete":
                        results.append(await outer.delete(args[0]))
                    elif op == "rpush":
                        results.append(await outer.rpush(args[0], *args[1:]))
                return results

        return _Pipe()


class FakeRedisService:
    """Stand-in for ``app.services.redis.RedisService`` used in unit tests."""

    def __init__(self) -> None:
        self.client = FakeRedisClient()
        # Behavior of SET NX EX — by default lets first SET win.
        self._kv_lock_owner: Dict[str, str] = {}

    async def get_client(self):
        return self.client

    async def get(self, key: str) -> Optional[str]:
        return self.client.kv.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: Optional[int] = None,
    ) -> bool:
        if nx and key in self.client.kv:
            return False
        self.client.kv[key] = value
        if ex is not None:
            self.client.expirations[key] = ex
        return True

    async def setex(
        self, key: str, value: str, ttl_seconds: Optional[int] = None
    ) -> bool:
        self.client.kv[key] = value
        if ttl_seconds is not None:
            self.client.expirations[key] = ttl_seconds
        return True

    async def delete(self, key: str) -> bool:
        existed = await self.client.delete(key)
        return existed > 0

    async def exists(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    async def run_script(self, script: str, keys: List[str], args: List[Any]) -> Any:
        """
        Hand-coded Lua-equivalent: detect the promoter script and the
        leader-election Lua bodies by content. Tests don't care about Lua
        execution — they care about the resulting state.
        """
        if "ZRANGEBYSCORE" in script and "ZREM" in script and "LPUSH" in script:
            schedule_key, ready_key = keys
            now_ms = int(args[0])
            batch = int(args[1])
            ids = await self.client.zrangebyscore(
                schedule_key, 0, now_ms, start=0, num=batch
            )
            moved = 0
            for i in ids:
                removed = await self.client.zrem(schedule_key, i)
                if removed == 1:
                    await self.client.lpush(ready_key, i)
                    moved += 1
            return moved

        if "EXPIRE" in script:
            (lock_key,) = keys
            self_id, ttl = args
            cur = self.client.kv.get(lock_key)
            if cur == self_id:
                self.client.expirations[lock_key] = int(ttl)
                return 1
            return 0

        if "DEL" in script:
            (lock_key,) = keys
            (self_id,) = args
            cur = self.client.kv.get(lock_key)
            if cur == self_id:
                await self.client.delete(lock_key)
                return 1
            return 0

        raise NotImplementedError(f"FakeRedisService.run_script: {script}")


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedisService:
    """
    Replace ``get_redis_service`` everywhere the dispatch module reads it.
    Returns the fake so tests can assert on its state.
    """
    fake = FakeRedisService()

    async def _get() -> FakeRedisService:
        return fake

    # Patch the canonical source plus every dispatch-side import alias.
    monkeypatch.setattr(redis_pkg, "get_redis_service", _get)

    for mod in (
        alerts_mod,
        ch_mod,
        leader_mod,
        prom_mod,
        queue_mod,
        recon_mod,
        worker_mod,
    ):
        monkeypatch.setattr(mod, "get_redis_service", _get, raising=False)

    return fake


# ---------------------------------------------------------------------------
# DispatchHarness — used by end-to-end and chaos tests
# ---------------------------------------------------------------------------


def make_lead(
    lead_id: str = "lead-1",
    status: LeadCallStatus = LeadCallStatus.BACKLOG,
    phone: str = "+15551234567",
    reseller_id: str = "res-1",
) -> LeadCallTracker:
    """Factory for a minimal BACKLOG lead suitable for dispatcher tests."""
    return LeadCallTracker(
        id=lead_id,
        reseller_id=reseller_id,
        template="welcome",
        template_id="tmpl-1",
        merchant_id="merchant-1",
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        payload={"customer_mobile_number": phone, "customer_name": "Test"},
        status=status,
        execution_mode=ExecutionMode.TELEPHONY,
    )


def make_config(reseller_id: str = "res-1") -> CallExecutionConfig:
    return CallExecutionConfig(
        id="cfg-1",
        initial_offset=0,
        retry_offset=60,
        call_start_time=dtime(0, 0),
        call_end_time=dtime(23, 59),
        max_retry=3,
        calling_provider=CallProvider.TWILIO,
        reseller_id=reseller_id,
        template="welcome",
        merchant_id="merchant-1",
        enable_calling=True,
        enforce_blacklist=True,
        inbound_block_action=InboundBlockAction.REJECT,
    )


def make_number(num_id: str = "num-1") -> TelephonyNumber:
    return TelephonyNumber(
        id=num_id,
        number="+15559999999",
        provider=CallProvider.TWILIO,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=2,
        reseller_id="res-1",
    )


class CallRecorder:
    """Captures every (target, from_number) the worker would dial."""

    def __init__(
        self,
        sid: Optional[str] = "CA-test-sid",
        raise_exc: Optional[Exception] = None,
    ):
        self.calls: List[Dict[str, Any]] = []
        self._sid = sid
        self._raise_exc = raise_exc

    def make_call(self, to: str, from_number: str, **kwargs: Any) -> Dict[str, Any]:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.calls.append({"to": to, "from": from_number, **kwargs})
        return {"sid": self._sid} if self._sid else {}

    async def make_call_async(
        self, to: str, from_number: str, **kwargs: Any
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.make_call, to, from_number, **kwargs)


class DispatchHarness:
    """
    Bundles the in-memory state + monkeypatched collaborators that the
    worker needs to run a full _iteration cycle without real DB/provider.

    Mutate ``leads``, ``call_recorder``, etc. between iterations to simulate
    races, failures, or status flips.
    """

    def __init__(self):
        self.leads: Dict[str, LeadCallTracker] = {}
        self.config = make_config()
        self.number = make_number()
        self.call_recorder = CallRecorder()
        # Lock state lives separately so tests can verify lock/unlock.
        self.locked_lead_ids: set[str] = set()
        # Track defer / completion / release events for assertions.
        self.deferred: List[tuple[str, int]] = []
        self.released_numbers: List[str] = []
        self.released_locks: List[str] = []
        self.completions: List[Dict[str, Any]] = []
        # Toggle behaviours.
        # ``pre_check_result`` is a convenience bool: True -> PROCEED,
        # False -> ABORT. For DEFER, set ``pre_check_decision`` directly
        # (it takes priority over ``pre_check_result`` when set).
        self.pre_check_result: bool = True
        self.pre_check_decision: Optional[PreCheckDecision] = None
        self.pre_check_defer_seconds: int = 0
        self.is_blacklisted: bool = False
        self.rate_limit_ok: bool = True
        self.rate_limit_defer_seconds: int = 0
        # Atomic-record (cross-lead race) toggle. When False, the harness
        # simulates the case where another concurrent worker filled the
        # bucket between our peek and our atomic record — the production
        # Lua's check-and-set rejects this attempt and the worker must
        # release channel + number + defer by the rate-limit window.
        self.rate_limit_record_accepts: bool = True
        self.rate_limit_record_defer_seconds: int = 0
        # Track each invocation so tests can assert on call-order
        # invariants (peek runs every dispatch; record runs only after
        # channel-token + number acquisition succeed; rejected records
        # are still observed here so we can pin the race-rejection path).
        self.rate_limit_peeks: list[dict[str, str]] = []
        self.rate_limit_records: list[dict[str, str]] = []
        # Templates whose greeting prep ran with the pre-dial
        # generate_realtime_opening_line flag (see the greeting mock below).
        self.opening_line_calls: List[Any] = []
        self.cas_succeeds: bool = True
        self.get_available_returns_none: bool = False
        # Captured alerts so tests can assert no-telephony-number throttled
        # alerts fired without needing a real Slack/Redis round-trip.
        self.no_telephony_number_alerts: list[dict[str, str]] = []
        # Failure injection — tests assign callables to raise on demand.
        self.get_lead_by_id_raises: Optional[Exception] = None
        self.acquire_lock_raises: Optional[Exception] = None

    def add_lead(self, lead: LeadCallTracker) -> None:
        self.leads[lead.id] = lead

    # ---- DB accessors -----------------------------------------------------

    async def get_lead_by_id(self, lead_id: str) -> Optional[LeadCallTracker]:
        if self.get_lead_by_id_raises is not None:
            raise self.get_lead_by_id_raises
        return self.leads.get(lead_id)

    async def acquire_lock_on_lead_by_id(
        self, lead_id: str, expected_status: LeadCallStatus
    ) -> Optional[LeadCallTracker]:
        if self.acquire_lock_raises is not None:
            raise self.acquire_lock_raises
        lead = self.leads.get(lead_id)
        if not lead or lead.status != expected_status:
            return None
        if lead_id in self.locked_lead_ids:
            return None
        self.locked_lead_ids.add(lead_id)
        return lead

    async def release_lock_on_lead_by_id(
        self, lead_id: str
    ) -> Optional[LeadCallTracker]:
        self.released_locks.append(lead_id)
        self.locked_lead_ids.discard(lead_id)
        return self.leads.get(lead_id)

    async def defer_lead_next_attempt_and_release_lock(
        self, lead_id: str, defer_seconds: int
    ) -> None:
        self.deferred.append((lead_id, defer_seconds))
        self.locked_lead_ids.discard(lead_id)
        lead = self.leads.get(lead_id)
        if lead is not None:
            lead.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                seconds=defer_seconds
            )

    async def update_lead_call_details(
        self,
        id: str,
        status: LeadCallStatus,
        call_id: str,
        call_initiated_time: datetime,
        telephony_number_id: str,
    ) -> Optional[LeadCallTracker]:
        if not self.cas_succeeds:
            return None
        lead = self.leads.get(id)
        if not lead:
            return None
        lead.status = status
        lead.call_id = call_id
        lead.call_initiated_time = call_initiated_time
        lead.telephony_number_id = telephony_number_id
        return lead

    async def update_lead_call_completion_details(
        self,
        id: str,
        status: LeadCallStatus,
        outcome: str,
        meta_data: Dict[str, Any],
        call_end_time: datetime,
    ) -> Optional[LeadCallTracker]:
        self.completions.append(
            {
                "id": id,
                "status": status,
                "outcome": outcome,
                "meta_data": meta_data,
            }
        )
        lead = self.leads.get(id)
        if lead:
            lead.status = status
            lead.outcome = outcome
        return lead

    async def get_template_by_id(self, template_id: str) -> Optional[Any]:
        # Returning None is fine — the worker skips greeting prep gracefully.
        return None

    async def is_number_blacklisted(self, phone: str, reseller_id: str) -> bool:
        return self.is_blacklisted

    # ---- managers.calls helpers -------------------------------------------

    async def _get_lead_config(
        self, lead: LeadCallTracker
    ) -> Optional[CallExecutionConfig]:
        return self.config

    def _is_within_calling_hours(self, config: CallExecutionConfig) -> bool:
        return True

    async def _run_pre_checks_for_lead(
        self, *args, **kwargs
    ) -> Tuple[PreCheckDecision, int]:
        if self.pre_check_decision is not None:
            return self.pre_check_decision, self.pre_check_defer_seconds
        if self.pre_check_result:
            return PreCheckDecision.PROCEED, 0
        return PreCheckDecision.ABORT, 0

    async def _get_available_number(
        self, config, template
    ) -> Optional[TelephonyNumber]:
        if self.get_available_returns_none:
            return None
        return self.number

    async def _acquire_number(self, number: TelephonyNumber) -> bool:
        return True

    async def _release_number(self, number_id: str, provider) -> None:
        self.released_numbers.append(number_id)

    # ---- telephony / rate-limit / greeting --------------------------------

    async def peek_outbound_rate_limit_and_alert(
        self, customer_phone: str, lead_id: str, reseller_id: str
    ) -> tuple[bool, int]:
        """Read-only rate-limit check. NEVER mutates state."""
        self.rate_limit_peeks.append(
            {"phone": customer_phone, "lead_id": lead_id, "reseller_id": reseller_id}
        )
        return (self.rate_limit_ok, self.rate_limit_defer_seconds)

    async def record_outbound_call_attempt(
        self, customer_phone: str, lead_id: str, reseller_id: str
    ) -> tuple[bool, int]:
        """Atomic check-and-record. Runs after channel-token + DB number
        acquisition, BEFORE make_call. Returns (allow, defer_seconds) so
        the worker can release resources and defer on race-rejection."""
        self.rate_limit_records.append(
            {"phone": customer_phone, "lead_id": lead_id, "reseller_id": reseller_id}
        )
        if not self.rate_limit_record_accepts:
            return (False, self.rate_limit_record_defer_seconds)
        return (True, 0)

    async def prepare_and_store_initial_greeting(
        self,
        lead_id: str,
        payload: Dict[str, Any],
        template: Any,
        generate_realtime_opening_line: bool = False,
    ) -> Optional[str]:
        if generate_realtime_opening_line:
            self.opening_line_calls.append(
                template.id if template is not None else None
            )
        return "ok"

    def apply_playground_overrides(
        self, lead: LeadCallTracker, template: Any, template_vars=None
    ) -> Any:
        # New API returns the template (possibly modified). Tests don't
        # exercise the playground path, so we just pass through.
        return template

    def get_voice_provider(self, provider, session, telephony_config):
        return self.call_recorder


@pytest.fixture
def harness(monkeypatch, fake_redis) -> DispatchHarness:
    """
    Wire a fresh DispatchHarness onto the ``worker`` module's import
    namespace. Each test gets a clean slate. Composes with ``fake_redis``.
    """
    h = DispatchHarness()

    # DB accessors — rebound on the worker module.
    monkeypatch.setattr(worker_mod, "get_lead_by_id", h.get_lead_by_id)
    monkeypatch.setattr(
        worker_mod, "acquire_lock_on_lead_by_id", h.acquire_lock_on_lead_by_id
    )
    monkeypatch.setattr(
        worker_mod, "release_lock_on_lead_by_id", h.release_lock_on_lead_by_id
    )
    monkeypatch.setattr(
        worker_mod,
        "defer_lead_next_attempt_and_release_lock",
        h.defer_lead_next_attempt_and_release_lock,
    )
    monkeypatch.setattr(
        worker_mod, "update_lead_call_details", h.update_lead_call_details
    )
    monkeypatch.setattr(
        worker_mod,
        "update_lead_call_completion_details",
        h.update_lead_call_completion_details,
    )
    monkeypatch.setattr(
        worker_mod,
        "get_template_by_id",
        h.get_template_by_id,
    )
    monkeypatch.setattr(worker_mod, "is_number_blacklisted", h.is_number_blacklisted)

    # managers.calls helpers.
    monkeypatch.setattr(worker_mod, "_get_lead_config", h._get_lead_config)
    monkeypatch.setattr(
        worker_mod, "_is_within_calling_hours", h._is_within_calling_hours
    )
    monkeypatch.setattr(
        worker_mod, "_run_pre_checks_for_lead", h._run_pre_checks_for_lead
    )
    monkeypatch.setattr(worker_mod, "_get_available_number", h._get_available_number)
    monkeypatch.setattr(worker_mod, "_acquire_number", h._acquire_number)
    monkeypatch.setattr(worker_mod, "_release_number", h._release_number)

    # Telephony + rate limit + greeting.
    monkeypatch.setattr(
        worker_mod,
        "peek_outbound_rate_limit_and_alert",
        h.peek_outbound_rate_limit_and_alert,
    )
    monkeypatch.setattr(
        worker_mod,
        "record_outbound_call_attempt",
        h.record_outbound_call_attempt,
    )

    async def _capture_no_telephony_number_alert(
        reseller_id: str, template: str, merchant_id: object
    ) -> None:
        h.no_telephony_number_alerts.append(
            {
                "reseller_id": reseller_id,
                "template": template,
                "merchant_id": str(merchant_id) if merchant_id is not None else "",
            }
        )

    monkeypatch.setattr(
        worker_mod, "raise_no_telephony_number", _capture_no_telephony_number_alert
    )
    monkeypatch.setattr(
        worker_mod,
        "prepare_and_store_initial_greeting",
        h.prepare_and_store_initial_greeting,
    )
    monkeypatch.setattr(
        worker_mod,
        "apply_playground_overrides",
        h.apply_playground_overrides,
    )
    monkeypatch.setattr(worker_mod, "get_voice_provider", h.get_voice_provider)

    return h


class AlwaysLeader:
    """LeaderElection stand-in for tests that drive the promoter manually."""

    is_leader = True

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None
