"""Break Package A, items 2–7 and 11: an acceptor coroutine's held v2 line through today's
``_dispatch``, on REAL Redis (the v2 Lua) with the dispatch harness for the DB/provider.

Every ticket runs through ``acceptor.dial_ticket`` (the production entry: its claim, the
``_dispatch`` wrapper's ``finally`` and the coroutine's own give-back all fire), then the test
checks where the lead and the line ended up:

* a BACKLOG lead is in exactly ONE place — its waiting room, or holding exactly one ticket
  (busy + lease + bb:tickets entry on one number) — never both, never two leases;
* no line is held that no ticket, call or lease explains (SCARD(busy) is fully accounted);
* the lead is never dialled from a number other than the one its ticket held.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional, cast

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod, worker as W
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    SCHEDULE_ZSET,
    reseller_paused_key,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    acceptor as A,
    reconcile as RC,
    routes as routes_mod,
    scripts,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    CallLimitUnavailable,
    CallLimitVerdict,
)
from app.schemas import LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead, make_number
from tests.breeze_buddy.dispatch.v2.conftest import (
    OWNER,
    _Svc,
    pop_ticket,
    seed_number,
    tickets_of,
)

pytestmark = pytest.mark.asyncio


T = "tmpl-1"
N1, N2 = "num-1", "num-2"


def now_ms() -> int:
    return int(time.time() * 1000)


def _async(value=None, exc: Optional[BaseException] = None):
    async def f(*a, **k):
        if exc is not None:
            raise exc
        return value

    return f


@pytest.fixture
async def env(rr, harness, monkeypatch):
    """Real Redis for v2 (scripts + routes), the harness for DB/provider, fake Redis for
    today's keys (tokens, schedule ZSET, pause keys). v2 is 'seen'."""
    svc = _Svc(rr)

    async def _get():
        return svc

    monkeypatch.setattr(routes_mod, "get_redis_service", _get)
    monkeypatch.setattr(queue_mod, "v2_seen", _async(True))

    numbers = {N1: make_number(N1), N2: make_number(N2)}
    numbers[N2].number = "+15550000002"
    pick = NS(number=N1)  # what today's number rule returns for the template right now

    async def avail(config, template):
        return numbers[pick.number]

    async def worker_avail(config, template):
        return numbers[pick.number]

    async def tpl(tid):
        return NS(
            id=tid,
            merchant_id="merchant-1",
            reseller_id="res-1",
            telephony_number_id=None,
        )

    monkeypatch.setattr(routes_mod, "get_template_by_id", tpl)
    monkeypatch.setattr(
        routes_mod, "get_call_execution_config_by_template_id", _async(harness.config)
    )
    monkeypatch.setattr(routes_mod, "_available_number", avail)
    monkeypatch.setattr(routes_mod, "_tiers", _async((set(), set())))
    monkeypatch.setattr(W, "_get_available_number", worker_avail)

    async def defer(lead_id, secs):
        # like the real accessor: the row with its new next_attempt_at
        await harness.defer_lead_next_attempt_and_release_lock(lead_id, secs)
        return harness.leads.get(lead_id)

    monkeypatch.setattr(W, "defer_lead_next_attempt_and_release_lock", defer)
    unrecorded: List[Any] = []

    async def unrecord(**k):
        unrecorded.append(k)

    monkeypatch.setattr(W, "unrecord_call_limit", unrecord)
    monkeypatch.setattr(W, "raise_call_limit_unavailable", _async(None))
    monkeypatch.setattr(W, "finish_lead_call_limit_reached", _async(None))

    real_spawn = W.spawn_background_task

    def spawn(coro, name=None):
        if name and name.startswith("crm-"):
            coro.close()  # the CRM mirror needs a DB; the greeting prewarm runs
            return None
        return real_spawn(coro, name=name)

    monkeypatch.setattr(W, "spawn_background_task", spawn)

    await seed_number(rr, N1, 1, {T: {"reseller": "res-1"}})
    lead = make_lead("L1")
    harness.add_lead(lead)
    return NS(
        rr=rr,
        h=harness,
        lead=lead,
        pick=pick,
        numbers=numbers,
        unrecorded=unrecorded,
    )


async def ticket(rr, lead: str = "L1", n: str = N1, due: Optional[int] = None) -> int:
    issued = await scripts.enqueue(T, lead, now_ms() - 1000 if due is None else due)
    assert issued == 1, issued
    got = await pop_ticket(n)  # an acceptor popped it; dial_ticket claims it
    assert got is not None and got[0] == lead
    return got[1]


async def where(rr, lead: str = "L1", nums=(N1, N2)) -> Dict[str, Any]:
    s: Dict[str, Any] = {"room": await rr.zscore(f"bb:q:{T}", lead)}
    s["busy"] = [n for n in nums if await rr.sismember(f"bb:busy:{n}", f"lead:{lead}")]
    s["lease"] = {}
    for n in nums:
        v = await rr.hget(f"bb:inflight:{n}", lead)
        if v:
            s["lease"][n] = json.loads(v)
    s["work"] = {n: (await tickets_of(rr, n)).count(lead) for n in nums}
    s["scard"] = {n: await rr.scard(f"bb:busy:{n}") for n in nums}
    return s


def assert_backlog_in_one_place(s: Dict[str, Any]) -> str:
    """A BACKLOG lead: in its room XOR holding exactly one ticket. Returns which."""
    if s["room"] is not None:
        assert s["busy"] == [] and s["lease"] == {}, s
        assert sum(s["work"].values()) == 0, s
        return "room"
    assert len(s["busy"]) == 1, f"lead in no room and holding no line (lost): {s}"
    n = s["busy"][0]
    assert list(s["lease"]) == [n], f"line held without its lease (leak): {s}"
    assert s["work"][n] == 1, s
    return "ticket"


async def run_ticket(tk: int, n: str = N1, lead: str = "L1") -> None:
    t = scripts.Ticket(n, lead, tk, T, now_ms())
    await A.dial_ticket(t, None, W.Worker(), asyncio.Event())


async def lease_owner(rr, n: str = N1, lead: str = "L1") -> str:
    return json.loads(await rr.hget(f"bb:inflight:{n}", lead))["owner"]


def room_due_in_s(s: Dict[str, Any]) -> float:
    return (s["room"] - now_ms()) / 1000.0


# -- item 2: rule 17 / race A — every re-queue while holding a line -----------------------


@pytest.mark.parametrize(
    "setup,defer_s",
    [
        (lambda e: setattr(e.h, "within_hours", False), 300),
        (
            lambda e: (
                setattr(e.h, "pre_check_decision", PreCheckDecision.DEFER),
                setattr(e.h, "pre_check_defer_seconds", 45),
            ),
            45,
        ),
        (
            lambda e: (
                setattr(e.h, "rate_limit_ok", False),
                setattr(e.h, "rate_limit_defer_seconds", 120),
            ),
            120,
        ),
        (
            lambda e: (
                setattr(e.h, "rate_limit_record_accepts", False),
                setattr(e.h, "rate_limit_record_defer_seconds", 90),
            ),
            90,
        ),
    ],
    ids=["hours_closed", "precheck_defer", "rate_limit_peek", "rate_limit_record"],
)
async def test_defer_with_held_line_lands_in_room_and_frees_line(env, setup, defer_s):
    tk = await ticket(env.rr)
    setup(env)
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room"
    assert defer_s - 5 < room_due_in_s(s) < defer_s + 5
    assert s["scard"][N1] == 0
    assert env.h.call_recorder.calls == []


async def test_pause_with_held_line_lands_in_room_30s_and_frees_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(W.Worker, "_reseller_paused", _async(True))
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room"
    assert 25 < room_due_in_s(s) < 35 and s["scard"][N1] == 0


async def test_paused_via_todays_key_with_held_line(env, harness):
    # the real pause check reads today's key through (fake) Redis
    import app.ai.voice.agents.breeze_buddy.dispatch.worker as worker_mod

    svc: Any = await worker_mod.get_redis_service()
    svc.client.kv[reseller_paused_key("res-1")] = "1"
    tk = await ticket(env.rr)
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room" and s["scard"][N1] == 0


async def test_call_limit_peek_unavailable_with_held_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(
        W, "merchant_call_limits", _async(exc=CallLimitUnavailable("rules down"))
    )
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room"
    assert 25 < room_due_in_s(s) < 35 and s["scard"][N1] == 0


async def test_call_limit_record_unavailable_with_held_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(W, "merchant_call_limits", _async(("rule",)))
    monkeypatch.setattr(W, "peek_call_limit", _async(CallLimitVerdict(allowed=True)))
    monkeypatch.setattr(
        W, "record_call_limit", _async(exc=CallLimitUnavailable("record down"))
    )
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room" and s["scard"][N1] == 0
    assert env.h.call_recorder.calls == []


async def test_call_limit_refused_finishes_and_frees_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(W, "merchant_call_limits", _async(("rule",)))
    monkeypatch.setattr(W, "peek_call_limit", _async(CallLimitVerdict(allowed=True)))
    monkeypatch.setattr(
        W, "record_call_limit", _async(CallLimitVerdict(allowed=False, count=2))
    )
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["room"] is None and s["busy"] == [] and s["lease"] == {}
    assert s["scard"][N1] == 0


@pytest.mark.parametrize(
    "setup",
    [
        lambda e: setattr(e.h, "is_blacklisted", True),
        lambda e: setattr(e.h, "pre_check_result", False),
        lambda e: setattr(e.h.config, "enable_calling", False),
        lambda e: setattr(e.h, "config", None),
        lambda e: e.h.leads["L1"].payload.update({"customer_mobile_number": None}),
        lambda e: setattr(e.h.leads["L1"], "status", LeadCallStatus.FINISHED),
        lambda e: e.h.leads.pop("L1"),
    ],
    ids=[
        "blacklisted",
        "precheck_abort",
        "calling_disabled",
        "no_config",
        "invalid_phone",
        "not_backlog",
        "not_found",
    ],
)
async def test_terminal_exits_free_the_line_and_never_requeue(env, setup):
    tk = await ticket(env.rr)
    setup(env)
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["room"] is None and s["busy"] == [] and s["lease"] == {}
    assert s["scard"][N1] == 0
    assert env.h.call_recorder.calls == []


async def test_number_unavailable_finishes_and_frees_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(W, "_get_available_number", _async(None))
    monkeypatch.setattr(W, "raise_no_telephony_number", _async(None))
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["room"] is None and s["busy"] == [] and s["scard"][N1] == 0
    assert env.h.completions[-1]["outcome"] == "NUMBER_UNAVAILABLE"


async def test_exception_before_dial_frees_line(env, monkeypatch):
    # today: an exception drops the pick; the backlog reconciler re-adds it
    tk = await ticket(env.rr)
    monkeypatch.setattr(W, "_run_pre_checks_for_lead", _async(exc=RuntimeError("boom")))
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["busy"] == [] and s["lease"] == {} and s["scard"][N1] == 0
    assert "L1" not in env.h.locked_lead_ids  # the lock was released


# -- item 3: double give-back / the finally net never frees a NEW ticket --------------------


async def test_requeue_due_now_reticket_survives_both_give_back_nets(env):
    """Defer 0 s: the explicit give-back frees the line, the re-queue re-tickets the
    lead at once (new ticket id); then the _dispatch finally AND the coroutine's
    return_line run with the OLD ticket — both must be no-ops."""
    tk1 = await ticket(env.rr)
    env.h.pre_check_decision = PreCheckDecision.DEFER
    env.h.pre_check_defer_seconds = 0
    await run_ticket(tk1)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "ticket"
    tk2 = s["lease"][N1]["tk"]
    assert tk2 != tk1
    # stray old-ticket give-backs, as many as anyone likes
    for _ in range(3):
        assert await scripts.return_line(N1, "L1", tk1, OWNER) == -1
        assert await scripts.return_line(N1, "L1", tk1, OWNER, not_placed=True) == -1
    assert await where(env.rr) == s
    # the new ticket dials exactly once
    env.h.pre_check_decision = None
    got = await pop_ticket(N1)
    assert got == ("L1", tk2)
    await run_ticket(tk2)
    assert len(env.h.call_recorder.calls) == 1
    s = await where(env.rr)
    assert s["busy"] == [N1] and s["lease"] == {} and s["room"] is None


async def test_finally_after_explicit_give_back_cannot_free_a_concurrent_ticket(env):
    """The wrapper's finally runs after the lead was re-ticketed by someone else
    (stale ticket): the new ticket's line stays held."""
    await seed_number(env.rr, N1, 2, {T: {"reseller": "res-1"}})
    tk1 = await ticket(env.rr)
    # reaper: tk1 is void, the lead is re-queued and re-ticketed (tk2)
    assert (await scripts.reap_lease(N1, "L1", tk1, T, now_ms() - 1) or 0) >= 1
    got = await pop_ticket(N1)
    assert got is not None and got[0] == "L1"
    tk2 = got[1]
    # the stale tk1 task now runs to a terminal exit (blacklisted)
    env.h.is_blacklisted = True
    await run_ticket(tk1)
    s = await where(env.rr)
    # the lead was FINISHED by the stale task (today's outcome) but tk2's line is intact:
    # its own task will see "not BACKLOG" and give it back.
    assert s["lease"][N1]["tk"] == tk2 and s["busy"] == [N1]
    await run_ticket(tk2)
    s = await where(env.rr)
    assert s["busy"] == [] and s["lease"] == {} and s["scard"][N1] == 0


# -- item 4: mark_dialling refused ----------------------------------------------------------


async def test_reaped_ticket_never_dials_unrecords_and_requeues(env, monkeypatch):
    monkeypatch.setattr(W, "merchant_call_limits", _async(("rule",)))
    monkeypatch.setattr(W, "peek_call_limit", _async(CallLimitVerdict(allowed=True)))
    monkeypatch.setattr(
        W, "record_call_limit", _async(CallLimitVerdict(allowed=True, member="m-7"))
    )
    tk1 = await ticket(env.rr)
    # reaped while the checks ran, as the reaper's claimed tier does it: the line freed,
    # the lead re-queued and unlocked
    real_record = W.record_call_limit

    async def record_then_reap(**k):
        assert (await scripts.reap_lease(N1, "L1", tk1, T, 0) or 0) >= 0
        await env.h.release_lock_on_lead_by_id("L1")
        return await real_record(**k)

    monkeypatch.setattr(W, "record_call_limit", record_then_reap)
    await run_ticket(tk1)
    assert env.h.call_recorder.calls == []
    assert [u["member"] for u in env.unrecorded] == ["m-7"]  # its own record, undone
    s = await where(env.rr)
    # the reaper's re-queue re-ticketed it at once (new id), never lost
    assert assert_backlog_in_one_place(s) == "ticket"
    assert s["lease"][N1]["tk"] != tk1 and s["scard"][N1] == 1
    # The stale task left the lead to the reaper (no second unlock or
    # re-queue of a lead it no longer owns)
    assert env.h.released_locks == ["L1"]


async def test_reaped_and_reissued_ticket_stale_task_leaves_new_ticket_alone(
    env, monkeypatch
):
    tk1 = await ticket(env.rr)
    holder: Dict[str, int] = {}
    real_mark = scripts.mark_dialling

    async def reap_reissue_then_mark(n, lead, tk, owner=""):
        if tk == tk1 and "tk2" not in holder:
            # the reaper: frees the line, re-queues and unlocks the lead (it was locked)
            await scripts.reap_lease(N1, "L1", tk1, T, now_ms() - 1)
            await W.release_lock_on_lead_by_id("L1")
            got = await pop_ticket(N1)
            assert got is not None
            holder["tk2"] = got[1]
        return await real_mark(n, lead, tk, owner)

    monkeypatch.setattr(scripts, "mark_dialling", reap_reissue_then_mark)
    await run_ticket(tk1)
    assert env.h.call_recorder.calls == []
    s = await where(env.rr)
    assert s["lease"][N1]["tk"] == holder["tk2"] and s["busy"] == [N1]
    assert s["room"] is None  # enqueue said -2: the new holder owns the lead
    await run_ticket(holder["tk2"])
    assert len(env.h.call_recorder.calls) == 1
    s = await where(env.rr)
    assert s["busy"] == [N1] and s["lease"] == {}


async def test_mark_dialling_lost_reply_fails_safe(env, monkeypatch):
    """Redis marked the lease dialling but the reply was lost: never dial, never free
    the line (a call is not provably absent); the stuck-dial reap owns it."""
    tk = await ticket(env.rr)
    real_mark = scripts.mark_dialling

    async def lost_reply(n, lead, t, owner=""):
        await real_mark(n, lead, t, owner)
        return scripts.Mark.REFUSED  # its one retry's reply was lost too

    monkeypatch.setattr(scripts, "mark_dialling", lost_reply)
    await run_ticket(tk)
    assert env.h.call_recorder.calls == []
    s = await where(env.rr)
    assert s["busy"] == [N1] and "dialling_ms" in s["lease"][N1]
    assert s["room"] is None


# -- item 5: not-placed exits ----------------------------------------------------------------


@pytest.mark.parametrize(
    "setup,due_s",
    [
        (lambda e: setattr(e.h.call_recorder, "_raise_exc", RuntimeError("down")), 5),
        (
            lambda e: setattr(e.h.call_recorder, "make_call", lambda *a, **k: None),
            10,
        ),
    ],
    ids=["make_call_raised", "provider_none"],
)
async def test_provider_said_not_placed_frees_the_marked_line(env, setup, due_s):
    tk = await ticket(env.rr)
    setup(env)
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room"
    assert due_s - 3 < room_due_in_s(s) < due_s + 3
    assert s["scard"][N1] == 0


async def test_sidless_reply_keeps_the_line_and_lease(env):
    tk = await ticket(env.rr)
    env.h.call_recorder._sid = None
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["busy"] == [N1] and "dialling_ms" in s["lease"][N1]
    # enqueue answered -2 (the lead still holds the line): it waits for the stuck reap
    assert s["room"] is None


async def test_exception_after_mark_dialling_keeps_the_line(env, monkeypatch):
    tk = await ticket(env.rr)
    monkeypatch.setattr(W, "update_lead_call_details", _async(exc=RuntimeError("db")))
    await run_ticket(tk)
    s = await where(env.rr)
    assert len(env.h.call_recorder.calls) == 1
    assert s["busy"] == [N1] and "dialling_ms" in s["lease"][N1]
    # a later stray plain give-back is refused too
    owner = await lease_owner(env.rr)
    assert await scripts.return_line(N1, "L1", tk, owner) == -3


async def test_cas_lost_after_dial_line_stays_with_the_call(env):
    tk = await ticket(env.rr)
    env.h.cas_succeeds = False
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["busy"] == [N1] and s["lease"] == {} and s["room"] is None


async def test_success_line_belongs_to_the_call(env):
    tk = await ticket(env.rr)
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["busy"] == [N1] and s["lease"] == {} and s["room"] is None
    assert env.h.leads["L1"].status == LeadCallStatus.PROCESSING
    assert str(env.h.leads["L1"].telephony_number_id) == N1
    assert len(env.h.call_recorder.calls) == 1


# -- item 6: lock failure ---------------------------------------------------------------------


async def test_lock_failure_backlog_requeues_30s_out_and_does_not_hot_loop(env):
    tk = await ticket(env.rr)
    env.h.locked_lead_ids.add("L1")  # a stale ticket's task holds the DB lock
    await run_ticket(tk)
    s = await where(env.rr)
    assert assert_backlog_in_one_place(s) == "room"
    assert 25 < room_due_in_s(s) < 35
    assert s["scard"][N1] == 0
    # nothing is re-issued now (no 1 Hz loop): match finds nothing due
    assert await scripts.match(N1) == 0


@pytest.mark.parametrize("status", [LeadCallStatus.PROCESSING, LeadCallStatus.FINISHED])
async def test_lock_failure_never_requeues_a_non_backlog_lead(env, status):
    tk = await ticket(env.rr)
    env.h.locked_lead_ids.add("L1")
    env.h.leads["L1"].status = status
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["room"] is None and s["lease"] == {} and s["scard"][N1] == 0


# -- item 7: number mismatch --------------------------------------------------------------------


async def test_mismatch_gives_back_old_line_and_requeues_on_new_v2_route(env):
    await seed_number(env.rr, N2, 1, {})
    tk = await ticket(env.rr)
    env.pick.number = N2  # the template's number moved while the ticket waited
    await run_ticket(tk)
    assert env.h.call_recorder.calls == []  # never dialled from the old number
    s = await where(env.rr)
    assert s["scard"][N1] == 0 and N1 not in s["lease"]
    assert await env.rr.hget(f"bb:route:{T}", "number") == N2
    assert await env.rr.sismember(f"bb:numtpl:{N2}", T)
    assert not await env.rr.sismember(f"bb:numtpl:{N1}", T)
    assert assert_backlog_in_one_place(s) == "ticket" and s["busy"] == [N2]
    # the new ticket dials from the new number
    got = await pop_ticket(N2)
    assert got is not None
    await run_ticket(got[1], n=N2)
    assert [c["from"] for c in env.h.call_recorder.calls] == [
        env.numbers[N2].number
    ] and len(env.h.call_recorder.calls) == 1
    assert str(env.h.leads["L1"].telephony_number_id) == N2


async def test_mismatch_to_a_legacy_number_goes_to_todays_schedule(env, fake_redis):
    tk = await ticket(env.rr)
    env.pick.number = N2  # N2 has no v2 mode: today's path owns it
    await run_ticket(tk)
    s = await where(env.rr)
    assert s["room"] is None and s["busy"] == [] and s["scard"][N1] == 0
    assert "L1" in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})
    assert env.h.call_recorder.calls == []


# -- item 8 (with real Redis): acceptor kill switch / cancel / stop -------------------------


def _acceptor(monkeypatch, enabled: bool = True) -> A.Acceptor:
    monkeypatch.setattr(A, "v2_seen", _async(True))
    monkeypatch.setattr(A.dyn_cfg, "BB_DISPATCH_ENABLED", _async(enabled))
    monkeypatch.setattr(A, "BB_V2_ACCEPT_DISABLED_SLEEP_S", 0)
    return A.Acceptor()


async def test_kill_switch_leaves_the_ticket_waiting_and_never_dials(env, monkeypatch):
    await scripts.enqueue(T, "L1", now_ms() - 1000)
    waiting = await env.rr.lrange("bb:tickets", 0, -1)
    await _acceptor(monkeypatch, enabled=False)._round()
    s = await where(env.rr)
    assert env.h.call_recorder.calls == []
    assert await env.rr.lrange("bb:tickets", 0, -1) == waiting
    assert assert_backlog_in_one_place(s) == "ticket"  # its line waits with it


async def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_cancel_after_mark_dialling_keeps_the_line(env):
    tk = await ticket(env.rr)
    gate = asyncio.Event()

    async def ringing(*a, **k):
        await gate.wait()
        return {"sid": "CA1"}

    env.h.call_recorder.make_call_async = ringing  # type: ignore[assignment]
    task = asyncio.create_task(run_ticket(tk))

    async def marked():
        v = await env.rr.hget(f"bb:inflight:{N1}", "L1")
        return bool(v) and "dialling_ms" in json.loads(v)

    await _wait_for(marked)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s = await where(env.rr)
    assert s["busy"] == [N1] and "dialling_ms" in s["lease"][N1]
    assert "L1" not in env.h.locked_lead_ids  # the lock is released


async def test_cancel_during_prewarm_frees_the_line(env, monkeypatch):
    tk = await ticket(env.rr)
    entered = asyncio.Event()

    async def slow_prewarm(**k):
        entered.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(
        W,
        "get_template_by_id",
        _async(NS(id=T, configurations=None, is_active=True, telephony_number_id=None)),
    )
    monkeypatch.setattr(W, "_prewarm_initial_greeting_with_retry", slow_prewarm)
    task = asyncio.create_task(run_ticket(tk))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s = await where(env.rr)
    # the line went back and the lead was re-queued at once: it is back in its room, or
    # (the line being free) already holds a NEW ticket; never the cancelled one
    if assert_backlog_in_one_place(s) == "ticket":
        assert s["lease"][N1]["tk"] != tk and "owner" not in s["lease"][N1]
    assert env.h.call_recorder.calls == []


async def test_stop_waits_for_the_running_ticket_without_cancelling(env, monkeypatch):
    await scripts.enqueue(T, "L1", now_ms() - 1000)
    gate, entered = asyncio.Event(), asyncio.Event()

    async def ringing(*a, **k):
        entered.set()
        await gate.wait()
        return {"sid": "CA1"}

    env.h.call_recorder.make_call_async = ringing  # type: ignore[assignment]
    acc = _acceptor(monkeypatch)
    acc.start()
    await asyncio.wait_for(entered.wait(), 5)
    (task,) = list(acc._inflight)
    await acc.stop(grace_s=0.2)
    assert not task.done() and not task.cancelled()
    gate.set()
    assert await asyncio.wait_for(task, 8) is True
    s = await where(env.rr)
    assert s["busy"] == [N1] and s["lease"] == {}  # dialled, lease cleared
    assert env.h.leads["L1"].status == LeadCallStatus.PROCESSING


# -- concurrency: a stale and a fresh ticket for the same lead, random interleavings ---------


@pytest.mark.parametrize("seed", list(range(16)))
async def test_stale_and_fresh_ticket_race_never_double_dials_or_loses(
    env, monkeypatch, seed
):
    rnd = random.Random(seed)

    def yields(fn):
        async def w(*a, **k):
            for _ in range(rnd.randint(0, 3)):
                await asyncio.sleep(0)
            return await fn(*a, **k)

        return w

    for name in (
        "get_lead_by_id",
        "acquire_lock_on_lead_by_id",
        "release_lock_on_lead_by_id",
        "update_lead_call_details",
        "defer_lead_next_attempt_and_release_lock",
        "_run_pre_checks_for_lead",
        "schedule_lead",
    ):
        monkeypatch.setattr(W, name, yields(getattr(W, name)))
    for name in ("mark_dialling", "return_line", "enqueue", "clear_lease"):
        monkeypatch.setattr(scripts, name, yields(getattr(scripts, name)))
    rec = env.h.call_recorder
    real_call = rec.make_call_async

    async def call(*a, **k):
        for _ in range(rnd.randint(0, 3)):
            await asyncio.sleep(0)
        return await real_call(*a, **k)

    rec.make_call_async = call  # type: ignore[assignment]
    if seed % 2:
        env.h.pre_check_decision = PreCheckDecision.DEFER  # re-queue now
        env.h.pre_check_defer_seconds = 0

    tk1 = await ticket(env.rr)
    await scripts.reap_lease(N1, "L1", tk1, T, now_ms() - 1)  # tk1 void, re-ticketed
    got = await pop_ticket(N1)
    assert got is not None
    tk2 = got[1]
    order = [run_ticket(tk1), run_ticket(tk2)]
    if rnd.random() < 0.5:
        order.reverse()
    await asyncio.gather(*order)
    # drain any ticket issued since (each defer-0 re-ticket), then allow dials
    env.h.pre_check_decision = None
    for _ in range(5):
        nxt = await pop_ticket(N1)
        if nxt is None:
            break
        await run_ticket(nxt[1])
    s = await where(env.rr)
    assert len(rec.calls) <= 1
    if rec.calls:
        assert env.h.leads["L1"].status == LeadCallStatus.PROCESSING
        assert s["busy"] == [N1] and s["lease"] == {} and s["room"] is None
        assert s["scard"][N1] == 1
    else:
        assert env.h.leads["L1"].status == LeadCallStatus.BACKLOG
        assert assert_backlog_in_one_place(s) == "room"
        assert s["scard"][N1] == 0


# -- the acceptor runs many coroutines on one pod --------------------------------------------


async def test_two_tickets_on_one_pod_each_give_back_their_own_line(env, monkeypatch):
    """Coordinator item 11: _dispatch keeps per-dispatch state on its Worker, so the
    acceptor gives each coroutine its own. Two leads deferred by their pre-check while
    both are in flight end up each in its room, with no line held."""
    await seed_number(env.rr, N1, 2, {T: {"reseller": "res-1"}})
    env.h.add_lead(make_lead("L2"))
    for lead in ("L1", "L2"):
        assert await scripts.enqueue(T, lead, now_ms() - 1000) == 1
    both_in, released = asyncio.Event(), []

    async def pre_checks(config, lead, template, session, **_):
        released.append(lead.id)
        if len(released) == 2:
            both_in.set()
        await both_in.wait()
        return PreCheckDecision.DEFER, 30

    monkeypatch.setattr(W, "_run_pre_checks_for_lead", pre_checks)
    acc = _acceptor(monkeypatch)
    await acc._round()
    await asyncio.wait(list(acc._inflight), timeout=5)
    assert acc.in_flight == 0
    for lead in ("L1", "L2"):
        s = await where(env.rr, lead)
        assert assert_backlog_in_one_place(s) == "room"
    assert await env.rr.scard(f"bb:busy:{N1}") == 0


async def test_a_redelivered_ticket_of_a_finished_lead_frees_its_line_once(
    env, monkeypatch
):
    """Coordinator item 14: a ticket whose pop was lost is re-pushed by the reaper; by then
    the merchant finished the lead. Its coroutine exits cleanly and the line is freed once.
    """
    await env.rr.sadd("bb:v2:active", N1)
    tk = await ticket(env.rr)  # popped; the pod died before claiming
    env.h.leads["L1"].status = LeadCallStatus.FINISHED
    lease = json.loads(await env.rr.hget(f"bb:inflight:{N1}", "L1"))
    lease["issued_ms"] = now_ms() - 31_000
    await env.rr.hset(f"bb:inflight:{N1}", "L1", json.dumps(lease))
    monkeypatch.setattr(RC, "get_lead_dispatch_states", _async({}))
    assert await RC.reap_leases() == 1  # back in bb:tickets, same ticket
    assert await pop_ticket(N1) == ("L1", tk)
    await run_ticket(tk)
    s = await where(env.rr)
    assert env.h.call_recorder.calls == []
    assert s["busy"] == [] and s["lease"] == {} and s["scard"][N1] == 0


# -- Swaroop's #1318 review: a dispatch the reaper took over never finishes the lead ------


async def test_a_reaped_dispatch_never_finishes_the_lead_on_a_call_limit_refusal(
    env, monkeypatch
):
    """Its checks outlasted the claimed tier: the reaper freed the line and re-queued the
    lead, which a new ticket may already be calling. The stale refusal must not finish it
    (that would free the live call's line)."""
    tk1 = await ticket(env.rr)

    async def peek_after_reap(**k):
        assert (await scripts.reap_lease(N1, "L1", tk1, T, 0) or 0) >= 0
        await env.h.release_lock_on_lead_by_id("L1")
        return CallLimitVerdict(allowed=False, count=2)

    finished = []

    async def finish(lead, verdict, session):
        finished.append(lead.id)

    monkeypatch.setattr(W, "merchant_call_limits", _async(("rule",)))
    monkeypatch.setattr(W, "peek_call_limit", peek_after_reap)
    monkeypatch.setattr(W, "finish_lead_call_limit_reached", finish)
    await run_ticket(tk1)
    assert finished == []
    assert env.h.call_recorder.calls == []


async def test_a_reaped_dispatch_never_finishes_the_lead_on_a_precheck_failure(
    env, monkeypatch
):
    tk1 = await ticket(env.rr)
    seen = []

    async def pre_checks(config, lead, template, session, still_ours=None):
        assert (await scripts.reap_lease(N1, "L1", tk1, T, 0) or 0) >= 0
        await env.h.release_lock_on_lead_by_id("L1")
        assert still_ours is not None
        seen.append(await still_ours())
        return PreCheckDecision.ABORT, 0

    monkeypatch.setattr(W, "_run_pre_checks_for_lead", pre_checks)
    await run_ticket(tk1)
    assert seen == [False]  # the runner is told it no longer owns the lead
    assert env.h.call_recorder.calls == []


async def test_the_precheck_runner_skips_the_finish_for_a_lead_no_longer_ours(
    monkeypatch,
):
    from types import SimpleNamespace as NS

    from app.ai.voice.agents.breeze_buddy.managers import calls as C

    failed = NS(
        should_proceed=False,
        failure_action=None,
        results=[],
        exports=None,
        summary=lambda: "x",
    )
    monkeypatch.setattr(C, "run_pre_checks", _async(failed))
    writes = []

    async def write(**k):
        writes.append(k)

    monkeypatch.setattr(C, "update_lead_call_completion_details", write)
    lead = cast(
        Any, NS(id="L1", metaData={}, payload={}, attempt_count=0, request_id="r")
    )
    config = cast(Any, NS(pre_checks=["c"]))

    async def no() -> bool:
        return False

    async def yes() -> bool:
        return True

    decision, _ = await C._run_pre_checks_for_lead(
        config, lead, None, None, still_ours=no
    )
    assert decision is PreCheckDecision.ABORT and writes == []
    decision, _ = await C._run_pre_checks_for_lead(
        config, lead, None, None, still_ours=yes
    )
    assert decision is PreCheckDecision.ABORT and len(writes) == 1


async def test_a_give_back_whose_first_reply_was_lost_still_owns_the_lead(
    env, monkeypatch
):
    """The first return_line ran but its reply was lost; the retry finds no lease. That
    is our own give-back, so the lead's lock and schedule stay this dispatch's."""
    tk1 = await ticket(env.rr)
    real = scripts.return_line
    calls = []

    async def lost_once(*a, **k):
        calls.append(1)
        result = await real(*a, **k)
        return None if len(calls) == 1 else result

    assert await scripts.claim(N1, "L1", tk1, OWNER)
    monkeypatch.setattr(W.v2_scripts, "return_line", lost_once)
    line = W._DispatchLine(W.ClaimedTicket(N1, tk1, OWNER, asyncio.Event()), "L1")
    await line.give_back()
    assert len(calls) == 2 and line.not_ours is False
