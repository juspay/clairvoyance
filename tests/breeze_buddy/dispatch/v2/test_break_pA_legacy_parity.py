"""Break Package A, item 1: while v2 has never been used, today's worker is byte-for-byte today's.

Every legacy exit of ``Worker._dispatch`` runs twice on identical fakes: once through the
BASE worker + queue (release 58e4d4e6, before v2, loaded from git) and once through HEAD.
The full trace — every collaborator call (DB, provider, tokens, number release, alerts,
call limits), every Redis command with its arguments (ZADD scores incl. jitter), and every
log line — must be identical. Every v2 entry point (scripts, routes, route invalidation)
is a booby trap that records any touch.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable, Dict, List, Optional

import pytest

import app.services.redis as redis_pkg
from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused
from app.ai.voice.agents.breeze_buddy.dispatch import (
    channel_semaphore as ch_mod,
    queue as queue_mod,
    reconcilers as recon_mod,
    worker as head_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    SCHEDULE_ZSET,
    channel_key,
    processing_list_for,
    reseller_paused_key,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    latch as latch_mod,
    routes as routes_mod,
    scripts as scripts_mod,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    CallLimitUnavailable,
    CallLimitVerdict,
)
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.outcomes import initiated_call_outcome, legacy_outcome
from tests.breeze_buddy.dispatch.conftest import (
    DispatchHarness,
    FakeRedisService,
    make_lead,
)

pytestmark = pytest.mark.asyncio

BASE_SHA = "58e4d4e6"  # release, before v2: the commit the v2 stack (#1313) sits on
REPO = Path(__file__).resolve().parents[4]
FIXED_NOW = datetime(2026, 10, 4, 6, 30, 0, 123456, tzinfo=timezone.utc)


def _load_base(path: str, name: str) -> types.ModuleType:
    try:
        src = subprocess.run(
            ["git", "show", f"{BASE_SHA}:{path}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except Exception as e:  # noqa: BLE001
        # Never skip: a parity proof that quietly stops running proves nothing
        # (review #1287 finding 8). CI checks out full history; after a rebase,
        # BASE_SHA must name the release commit this branch sits on.
        pytest.fail(f"parity base {BASE_SHA}:{path} not available from git: {e}")
    mod = types.ModuleType(name)
    exec(compile(src, f"<{path}@{BASE_SHA}>", "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def base():
    b = NS(
        worker=_load_base(
            "app/ai/voice/agents/breeze_buddy/dispatch/worker.py", "bb_base_worker"
        ),
        queue=_load_base(
            "app/ai/voice/agents/breeze_buddy/dispatch/queue.py", "bb_base_queue"
        ),
    )
    # really the pre-Package-A code
    assert not hasattr(b.worker, "HeldLine") and not hasattr(b.queue, "v2_seen")
    return b


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _san(v: Any) -> Any:
    if isinstance(v, datetime):
        return ("dt", v.isoformat())
    if isinstance(v, (list, tuple)):
        return type(v).__name__, [_san(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _san(x) for k, x in v.items()}
    if isinstance(v, BaseException):
        return ("exc", type(v).__name__, str(v))
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "id"):
        return (type(v).__name__, str(getattr(v, "id")))
    if hasattr(v, "value"):
        return ("enum", str(getattr(v, "value")))
    return type(v).__name__


def _tracer(
    name: str, fn: Callable, trace: list, tids: list, facts: Optional[list] = None
) -> Callable:
    def rec(a, k):
        if name == "schedule_lead" and "template_id" in k:
            tids.append(k["template_id"])
            k = {x: y for x, y in k.items() if x != "template_id"}
        if facts is not None and "call_outcome" in k:
            # The call outcome facts ride beside today's writes (docs/
            # CALL_OUTCOMES.md, PR 1): recorded apart, so the trace still
            # proves every statement is otherwise today's.
            facts.append((name, k.get("outcome"), k["call_outcome"]))
            k = {x: y for x, y in k.items() if x != "call_outcome"}
        trace.append(("call", name, _san(a), _san(k)))

    if inspect.iscoroutinefunction(fn):

        async def t(*a, **k):
            rec(a, k)
            return await fn(*a, **k)

    else:

        def t(*a, **k):
            rec(a, k)
            return fn(*a, **k)

    return t


class _Log:
    def __init__(self, trace: list, bound: Any = None):
        self._trace = trace
        self._bound = bound

    def bind(self, **kw):
        return _Log(self._trace, ("bind", sorted((k, str(v)) for k, v in kw.items())))

    def opt(self, **kw):
        return _Log(self._trace, ("opt", sorted((k, str(v)) for k, v in kw.items())))

    def __getattr__(self, level: str):
        def f(msg, *a, **k):
            self._trace.append(("log", level, self._bound, str(msg)))

        return f


def _trace_redis(fake: FakeRedisService, trace: list) -> None:
    for obj, names, tag in (
        (
            fake.client,
            (
                "zadd zrem zscore zcard rpush lpush lpop lrem llen lrange blpop "
                "delete exists scan"
            ).split(),
            "redis",
        ),
        (fake, "get set setex delete exists run_script".split(), "redis-svc"),
    ):
        for n in names:
            orig = getattr(obj, n)

            def mk(orig=orig, n=n, tag=tag):
                async def w(*a, **k):
                    trace.append((tag, n, _san(a), _san(k)))
                    return await orig(*a, **k)

                return w

            setattr(obj, n, mk())


class _Boom:
    """Any attribute is a function that records the touch and raises."""

    def __init__(self, touched: list, label: str):
        self._touched = touched
        self._label = label

    def __getattr__(self, name):
        touched, label = self._touched, self._label

        async def boom(*a, **k):
            touched.append(f"{label}.{name}")
            raise AssertionError(f"v2 touched while unseen: {label}.{name}")

        return boom


def _booby_trap_v2(monkeypatch, touched: list) -> None:
    def boom(label):
        async def f(*a, **k):
            touched.append(label)
            raise AssertionError(f"v2 touched while unseen: {label}")

        return f

    monkeypatch.setattr(head_mod, "v2_scripts", _Boom(touched, "worker.v2_scripts"))
    monkeypatch.setattr(queue_mod, "v2_scripts", _Boom(touched, "queue.v2_scripts"))
    monkeypatch.setattr(head_mod, "_invalidate_route", boom("worker._invalidate_route"))
    monkeypatch.setattr(queue_mod, "_ensure_route", boom("queue._ensure_route"))
    monkeypatch.setattr(queue_mod, "_invalidate_route", boom("queue._invalidate_route"))
    for n in (
        "ensure_route",
        "invalidate_route",
        "number_mode_or_none",  # the mode read (number_mode / is_v2_accounted: removed)
        "template_is_v2_accounted",
        "refresh_number",
    ):
        monkeypatch.setattr(routes_mod, n, boom(f"routes.{n}"))
    monkeypatch.setattr(scripts_mod, "v2_redis", boom("scripts.redis"))
    monkeypatch.setattr(latch_mod, "get_redis_service", boom("latch.redis"))


HARNESS_NAMES = [
    "get_lead_by_id",
    "acquire_lock_on_lead_by_id",
    "release_lock_on_lead_by_id",
    "update_lead_call_details",
    "update_lead_call_completion_details",
    "get_template_by_id",
    "is_number_blacklisted",
    "_get_lead_config",
    "_is_within_calling_hours",
    "_run_pre_checks_for_lead",
    "_get_available_number",
    "_acquire_number",
    "_release_number",
    "peek_outbound_rate_limit_and_alert",
    "record_outbound_call_attempt",
    "prepare_and_store_initial_greeting",
    "apply_playground_overrides",
    "get_voice_provider",
    "merchant_call_limits",
]


def _default_fakes(h: DispatchHarness, record: list) -> Dict[str, Callable]:
    async def defer(lead_id, defer_seconds):
        # Like the real accessor: returns the row with the DB's next_attempt_at.
        await h.defer_lead_next_attempt_and_release_lock(lead_id, defer_seconds)
        return NS(next_attempt_at=FIXED_NOW + timedelta(seconds=defer_seconds))

    async def noop(*a, **k):
        return None

    async def peek_call_limit(**k):
        return CallLimitVerdict(allowed=True)

    async def record_call_limit(**k):
        return CallLimitVerdict(allowed=True, member="m-1")

    def spawn(coro, name=None):
        coro.close()
        record.append(("spawn", name))

    return {
        "defer_lead_next_attempt_and_release_lock": defer,
        "raise_no_telephony_number": noop,
        "raise_call_limit_unavailable": noop,
        "finish_lead_call_limit_reached": noop,
        "peek_call_limit": peek_call_limit,
        "record_call_limit": record_call_limit,
        "unrecord_call_limit": noop,
        "spawn_background_task": spawn,
    }


async def _run(
    worker_mod: types.ModuleType,
    queue_impl: types.ModuleType,
    scenario: Callable,
    monkeypatch,
) -> NS:
    trace: List[Any] = []
    tids: List[Any] = []
    facts: List[Any] = []
    h = DispatchHarness()
    lead = make_lead("L1")
    lead.next_attempt_at = FIXED_NOW  # due on the frozen clock the worker sees
    h.add_lead(lead)
    fake = FakeRedisService()
    fake.client.lists[channel_key("num-1")] = ["tok-1", "tok-2"]
    impl: Dict[str, Callable] = {n: getattr(h, n) for n in HARNESS_NAMES}
    impl.update(_default_fakes(h, trace))
    impl.update(scenario(h, fake) or {})
    _trace_redis(fake, trace)

    async def _get():
        return fake

    for m in (redis_pkg, queue_mod, ch_mod, worker_mod, queue_impl):
        monkeypatch.setattr(m, "get_redis_service", _get, raising=False)
    impl["acquire_channel_token"] = ch_mod.acquire_channel_token
    impl["release_channel_token"] = ch_mod.release_channel_token
    impl["schedule_lead"] = queue_impl.schedule_lead
    impl["is_dispatchable"] = queue_impl.is_dispatchable
    for name, fn in impl.items():
        monkeypatch.setattr(worker_mod, name, _tracer(name, fn, trace, tids, facts))
    monkeypatch.setattr(worker_mod, "logger", _Log(trace))
    monkeypatch.setattr(worker_mod, "datetime", _FrozenDT)
    random.seed(20261004)
    w = worker_mod.Worker(worker_uuid="w-parity")
    raised = None
    try:
        ret = await w._dispatch("L1", None)
    except BaseException as e:  # noqa: BLE001 — CancelledError is part of the parity
        raised = type(e).__name__
        ret = None
    return NS(
        trace=trace,
        tids=tids,
        facts=facts,
        raised=raised,
        ret=ret,
        calls=list(h.call_recorder.calls),
        released_locks=list(h.released_locks),
        released_numbers=list(h.released_numbers),
        deferred=list(h.deferred),
        completions=[
            (c["id"], c["status"], c["outcome"], c["meta_data"]) for c in h.completions
        ],
        locked=set(h.locked_lead_ids),
        lead=(h.leads.get("L1").status if h.leads.get("L1") else None),
        redis=(
            {k: dict(v) for k, v in fake.client.zsets.items()},
            {k: list(v) for k, v in fake.client.lists.items()},
            dict(fake.client.kv),
        ),
    )


# -- scenarios: every exit of today's _dispatch -------------------------------------------


def _async(value=None, exc: Optional[BaseException] = None):
    async def f(*a, **k):
        if exc is not None:
            raise exc
        return value

    return f


def _template():
    return NS(
        id="tmpl-1", configurations=None, is_active=True, telephony_number_id=None
    )


def s_success(h, f):
    return None


def s_blacklisted(h, f):
    h.is_blacklisted = True


def s_hours_closed(h, f):
    h.within_hours = False


def s_precheck_abort(h, f):
    h.pre_check_result = False


def s_precheck_defer(h, f):
    h.pre_check_decision = PreCheckDecision.DEFER
    h.pre_check_defer_seconds = 45


def s_rate_peek_defer(h, f):
    h.rate_limit_ok = False
    h.rate_limit_defer_seconds = 120


def s_no_token(h, f):
    f.client.lists[channel_key("num-1")] = []


def s_db_denied(h, f):
    return {"_acquire_number": _async(False)}


def s_invalid_phone(h, f):
    h.leads["L1"].payload = {"customer_mobile_number": None}


def s_rate_record_rejected(h, f):
    h.rate_limit_record_accepts = False
    h.rate_limit_record_defer_seconds = 90


def s_make_call_raises(h, f):
    h.call_recorder._raise_exc = RuntimeError("provider down")


def s_provider_none(h, f):
    h.call_recorder.make_call = lambda *a, **k: None  # type: ignore[assignment]


def s_sidless(h, f):
    h.call_recorder._sid = None


def s_cas_lost(h, f):
    h.cas_succeeds = False


def s_paused(h, f):
    f.client.kv[reseller_paused_key("res-1")] = "1"


def s_lock_failure(h, f):
    h.locked_lead_ids.add("L1")


def s_number_unavailable(h, f):
    h.get_available_returns_none = True


def s_no_config(h, f):
    h.config = None


def s_calling_disabled(h, f):
    h.config.enable_calling = False


def s_not_found(h, f):
    del h.leads["L1"]


def s_not_backlog(h, f):
    h.leads["L1"].status = LeadCallStatus.FINISHED


def s_account_refused(h, f):
    async def refuse(*a, **k):
        raise AccountRefused("no account")

    h.call_recorder.use_template_credentials = refuse  # type: ignore[assignment]


def s_prewarm_cancelled(h, f):
    return {
        "get_template_by_id": _async(_template()),
        "_prewarm_initial_greeting_with_retry": _async(exc=asyncio.CancelledError()),
    }


def s_with_template_success(h, f):
    return {
        "get_template_by_id": _async(_template()),
        "_prewarm_initial_greeting_with_retry": _async(None),
    }


def _limits(**over):
    d = {"merchant_call_limits": _async(("rule",))}
    d.update(over)
    return d


def s_cl_peek_unavailable(h, f):
    return {"merchant_call_limits": _async(exc=CallLimitUnavailable("x", capped=True))}


def s_cl_peek_refused(h, f):
    return _limits(peek_call_limit=_async(CallLimitVerdict(allowed=False, count=3)))


def s_cl_record_unavailable(h, f):
    return _limits(record_call_limit=_async(exc=CallLimitUnavailable("y")))


def s_cl_record_refused(h, f):
    return _limits(record_call_limit=_async(CallLimitVerdict(allowed=False)))


def s_cl_success(h, f):
    return _limits()


def s_cl_make_call_raises(h, f):
    h.call_recorder._raise_exc = RuntimeError("provider down")
    return _limits()


def s_cl_provider_none(h, f):
    h.call_recorder.make_call = lambda *a, **k: None  # type: ignore[assignment]
    return _limits()


def s_cl_sidless(h, f):
    h.call_recorder._sid = None
    return _limits()


def s_update_raises_after_dial(h, f):
    return {"update_lead_call_details": _async(exc=RuntimeError("db down"))}


def s_get_lead_raises(h, f):
    h.get_lead_by_id_raises = RuntimeError("db down")


SCENARIOS = [
    s_success,
    s_blacklisted,
    s_hours_closed,
    s_precheck_abort,
    s_precheck_defer,
    s_rate_peek_defer,
    s_no_token,
    s_db_denied,
    s_invalid_phone,
    s_rate_record_rejected,
    s_make_call_raises,
    s_provider_none,
    s_sidless,
    s_cas_lost,
    s_paused,
    s_lock_failure,
    s_number_unavailable,
    s_no_config,
    s_calling_disabled,
    s_not_found,
    s_not_backlog,
    s_account_refused,
    s_prewarm_cancelled,
    s_with_template_success,
    s_cl_peek_unavailable,
    s_cl_peek_refused,
    s_cl_record_unavailable,
    s_cl_record_refused,
    s_cl_success,
    s_cl_make_call_raises,
    s_cl_provider_none,
    s_cl_sidless,
    s_update_raises_after_dial,
    s_get_lead_raises,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.__name__ for s in SCENARIOS])
async def test_legacy_dispatch_is_identical_to_base(scenario, base, monkeypatch):
    touched: List[str] = []
    seen_calls: List[Any] = []

    async def unseen():
        seen_calls.append(1)
        return False

    monkeypatch.setattr(queue_mod, "v2_seen", unseen)
    _booby_trap_v2(monkeypatch, touched)

    old = await _run(base.worker, base.queue, scenario, monkeypatch)
    new = await _run(head_mod, queue_mod, scenario, monkeypatch)

    assert touched == [], f"v2 touched on today's path: {touched}"
    assert new.trace == old.trace
    assert new.raised == old.raised
    for field in (
        "calls",
        "released_locks",
        "released_numbers",
        "deferred",
        "completions",
        "locked",
        "lead",
        "redis",
    ):
        assert getattr(new, field) == getattr(old, field), field
    # every re-queue of the lead now names its template (T5), and only that changed
    assert all(t == "tmpl-1" for t in new.tids)
    assert len(new.tids) == len([e for e in new.trace if e[1] == "schedule_lead"])
    # the call outcome facts: a finished lead's give back its word, and the
    # dial records the set-up
    assert old.facts == []
    writes = ("update_lead_call_completion_details", "update_lead_call_details")
    assert len(new.facts) == len([e for e in new.trace if e[1] in writes])
    for name, word, call_outcome in new.facts:
        if name == "update_lead_call_details":
            assert call_outcome == initiated_call_outcome()
        else:
            assert legacy_outcome(call_outcome) == word
    # the new return value is the only other visible change: True only when the
    # provider gave a SID (the PROCESSING write was attempted), else False
    got_sid = any(e[1] == "update_lead_call_details" for e in new.trace)
    assert new.ret is (got_sid if new.raised is None else None)


async def test_parity_harness_detects_a_changed_give_back(base, monkeypatch):
    """Self-check: the trace comparison is sensitive — dropping today's number
    release at one give-back site makes it fail."""
    touched: List[str] = []
    monkeypatch.setattr(queue_mod, "v2_seen", _async(False))
    _booby_trap_v2(monkeypatch, touched)
    old = await _run(base.worker, base.queue, s_invalid_phone, monkeypatch)

    def s_mutant(h, f):
        s_invalid_phone(h, f)
        return {"_release_number": _async(None)}

    new = await _run(head_mod, queue_mod, s_mutant, monkeypatch)
    assert new.released_numbers != old.released_numbers


# -- the other callers on today's path ------------------------------------------------------


async def test_schedule_lead_unseen_matches_base_score_and_touches_no_v2(
    base, monkeypatch, fake_redis
):
    touched: List[str] = []
    _booby_trap_v2(monkeypatch, touched)
    monkeypatch.setattr(queue_mod, "v2_seen", _async(False))
    monkeypatch.setattr(base.queue, "get_redis_service", _async(fake_redis))
    when = FIXED_NOW + timedelta(seconds=7)
    random.seed(99)
    assert await base.queue.schedule_lead("B1", when) is True
    random.seed(99)
    assert await queue_mod.schedule_lead("B1x", when, template_id="tmpl-1") is True
    z = fake_redis.client.zsets[SCHEDULE_ZSET]
    assert z["B1"] == z["B1x"]  # same jitter draw, same score
    assert touched == []


async def test_latch_unseen_does_no_redis_or_config_read_inside_its_window(
    monkeypatch, fake_redis
):
    """With the latch freshly checked and false, schedule_lead does no v2 I/O at all."""
    touched: List[str] = []
    _booby_trap_v2(monkeypatch, touched)
    reads: List[str] = []

    async def cfg():
        reads.append("cfg")
        return False

    monkeypatch.setattr(latch_mod.dyn_cfg, "BB_DISPATCH_V2_ENABLED", cfg)
    latch_mod._reset_for_tests()
    import time as _time

    monkeypatch.setattr(latch_mod, "_checked_at", _time.monotonic())
    for i in range(50):
        assert (
            await queue_mod.schedule_lead(f"L{i}", FIXED_NOW, template_id="T") is True
        )
    assert reads == [] and touched == []
    latch_mod._reset_for_tests()


async def test_legacy_reconciler_unseen_zadds_every_row_and_touches_no_v2(
    monkeypatch, fake_redis
):
    touched: List[str] = []
    _booby_trap_v2(monkeypatch, touched)
    monkeypatch.setattr(recon_mod, "v2_seen", _async(False))

    async def rows(*a, **k):
        return [("A", "r", 1000, "T-v2"), ("B", "r", 2000, None), ("C", "r", 3000, "T")]

    monkeypatch.setattr(recon_mod, "get_unscheduled_backlog_leads", rows)
    await recon_mod.reconcile_backlog_to_zset()
    assert fake_redis.client.zsets[SCHEDULE_ZSET] == {"A": 1000, "B": 2000, "C": 3000}
    assert touched == []


async def test_reap_stuck_processing_lists_unseen_passes_template_and_zadds(
    monkeypatch, fake_redis
):
    touched: List[str] = []
    _booby_trap_v2(monkeypatch, touched)
    monkeypatch.setattr(queue_mod, "v2_seen", _async(False))
    lead = make_lead("R1")
    lead.next_attempt_at = FIXED_NOW
    fake_redis.client.lists[processing_list_for("dead-worker")] = ["R1"]
    monkeypatch.setattr(recon_mod, "get_lead_by_id", _async(lead))
    seen: List[Any] = []
    real = recon_mod.schedule_lead

    async def spy(*a, **k):
        seen.append(k.get("template_id"))
        return await real(*a, **k)

    monkeypatch.setattr(recon_mod, "schedule_lead", spy)
    await recon_mod.reap_stuck_processing_lists()
    assert seen == ["tmpl-1"]
    assert "R1" in fake_redis.client.zsets[SCHEDULE_ZSET]
    assert touched == []
