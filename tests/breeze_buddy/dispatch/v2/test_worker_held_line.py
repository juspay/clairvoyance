"""``_dispatch`` with a v2 line already held by an acceptor coroutine (design card §4)."""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as W
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import throttle as v2_throttle
from app.schemas import LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead, make_number

pytestmark = pytest.mark.asyncio

HELD = W.ClaimedTicket("num-1", 1, "own-1", asyncio.Event())  # the harness's number
THROTTLED = {"status": "throttled", "sid": None, "retry_after_s": None}
TEMPLATE = NS(
    id="tmpl-1", configurations=None, is_active=True, telephony_number_id=None
)


@pytest.fixture
def v2(monkeypatch, harness):
    """A harness with a BACKLOG lead, v2 scripts mocked, and every give-back recorded."""
    order = []

    async def _return_line(*a, **k):
        # each test asserts the owner where it matters (test_give_back_names_its_owner)
        order.append(
            ("give_back", a[:3], {"not_placed": True} if k["not_placed"] else {})
        )
        return 0

    async def _schedule(lead_id, when, jitter_ms=None, template_id=None):
        order.append(("schedule", lead_id, template_id, when))
        return True

    m = NS(
        order=order,
        h=harness,
        lead=make_lead("L1"),
        worker=W.Worker(worker_uuid="w-v2"),
        return_line=AsyncMock(side_effect=_return_line),
        mark=AsyncMock(return_value=W.v2_scripts.Mark.DIAL),
        schedule=AsyncMock(side_effect=_schedule),
        invalidate=AsyncMock(),
        acquire_token=AsyncMock(),
        acquire_db=AsyncMock(),
    )
    harness.add_lead(m.lead)
    monkeypatch.setattr(W.v2_scripts, "return_line", m.return_line)
    monkeypatch.setattr(W.v2_scripts, "mark_dialling", m.mark)
    monkeypatch.setattr(W, "schedule_lead", m.schedule)
    monkeypatch.setattr(W, "_invalidate_route", m.invalidate)
    monkeypatch.setattr(W, "acquire_channel_token", m.acquire_token)
    monkeypatch.setattr(W, "_acquire_number", m.acquire_db)
    # the 429 loop's waits move a fake clock (no real sleeps) and see the stop signal
    clock = NS(now=0.0)

    async def _wait(stopping, seconds):
        clock.now += seconds
        return stopping.is_set()

    monkeypatch.setattr(
        W, "_THROTTLE", v2_throttle.Throttle(clock=lambda: clock.now, wait=_wait)
    )
    return m


async def test_held_dials_of_one_template_read_it_config_and_number_once(
    v2, monkeypatch
):
    # spec §4.10: 3 DB reads per dial become 3 per template per BB_V2_DIAL_MEMO_TTL_S
    template = AsyncMock(return_value=TEMPLATE)
    config = AsyncMock(return_value=v2.h.config)
    number = AsyncMock(return_value=v2.h.number)
    monkeypatch.setattr(W, "get_template_by_id", template)
    monkeypatch.setattr(W, "_get_lead_config", config)
    monkeypatch.setattr(W, "_get_available_number", number)
    v2.h.add_lead(make_lead("L2"))
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    second = W.ClaimedTicket("num-1", 2, "own-2", asyncio.Event())
    assert await W.Worker(worker_uuid="w-2")._dispatch("L2", None, held=second)
    assert [m.await_count for m in (template, config, number)] == [1, 1, 1]


async def test_todays_path_never_uses_the_memo(harness, monkeypatch):
    template = AsyncMock(return_value=None)
    monkeypatch.setattr(W, "get_template_by_id", template)
    memo = AsyncMock(side_effect=AssertionError("memo on today's path"))
    monkeypatch.setattr(W._DIAL_MEMO, "get", memo)
    harness.add_lead(make_lead("L9"))
    await W.Worker(worker_uuid="w-today")._dispatch("L9", None)
    assert template.await_count == 1


def _gave_back(v2, **kw):
    return [o for o in v2.order if o[0] == "give_back"]


async def test_held_line_skips_token_and_db_gate_and_dials(v2):
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    v2.acquire_token.assert_not_awaited()
    v2.acquire_db.assert_not_awaited()
    v2.mark.assert_awaited_once_with("num-1", "L1", 1, "own-1")
    assert len(v2.h.call_recorder.calls) == 1
    assert v2.h.released_numbers == []
    assert _gave_back(v2) == []  # the line now belongs to the call


async def test_blacklisted_lead_gives_the_held_line_back(v2):
    v2.h.is_blacklisted = True
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {})]
    assert v2.h.call_recorder.calls == []


async def test_number_mismatch_gives_back_reresolves_and_requeues(v2):
    assert (
        await v2.worker._dispatch(
            "L1", None, held=W.ClaimedTicket("old", 4, "own-1", asyncio.Event())
        )
        is False
    )
    assert v2.order[0] == ("give_back", ("old", "L1", 4), {})
    assert v2.order[1][:3] == ("schedule", "L1", "tmpl-1")
    v2.invalidate.assert_awaited_once_with("tmpl-1")
    assert "L1" in v2.h.released_locks
    assert v2.h.call_recorder.calls == []


@pytest.mark.parametrize("pinned", ["old", None], ids=["pinned", "pool"])
async def test_a_repinned_template_dials_on_its_new_number_after_one_give_back(
    v2, pinned
):
    # The memo still named the old number after the template moved (a pin
    # edited, or a pool number withdrawn and on_number_saved re-resolving the route), so
    # every ticket on the new route mismatched, gave its line back and was re-issued, a
    # hot loop for up to BB_V2_DIAL_MEMO_TTL_S. Both kinds resolve through the memo's
    # ("num", template) entry; the harness's number lookup stands in for either.
    TEMPLATE.telephony_number_id = pinned
    v2.h.number = make_number("old")
    for lead in ("L2", "L3"):
        v2.h.add_lead(make_lead(lead))
    on_old = W.ClaimedTicket("old", 1, "own-1", asyncio.Event())
    assert await v2.worker._dispatch("L1", None, held=on_old) is True  # memo: "old"
    v2.h.number = make_number("num-1")  # the template now dials from num-1
    on_new = W.ClaimedTicket("num-1", 2, "own-2", asyncio.Event())
    assert await W.Worker(worker_uuid="w-2")._dispatch("L2", None, held=on_new) is False
    assert _gave_back(v2) == [("give_back", ("num-1", "L2", 2), {})]
    again = W.ClaimedTicket("num-1", 3, "own-3", asyncio.Event())
    assert await W.Worker(worker_uuid="w-3")._dispatch("L3", None, held=again) is True
    assert len(_gave_back(v2)) == 1  # one bounce, then it dials on num-1
    TEMPLATE.telephony_number_id = None


async def test_one_lead_ticketed_on_two_numbers_dials_once(v2, monkeypatch):
    # The template moved from num-1 to num-2 while L1 held a ticket on num-1,
    # and the backlog job re-enqueued L1 on num-2: two leases for one lead, each on a pod
    # whose memo matches its own ticket. The lead's row lets one dial: Plivo's dial row
    # (BACKLOG -> PROCESSING) is written before the request, so the other finds the lead
    # no longer BACKLOG and gives its line back.
    from app.schemas import CallProvider

    def plivo(num_id):
        n = make_number(num_id)
        n.provider = CallProvider.PLIVO
        return n

    v2.h.number = plivo("num-1")
    on_old = W.ClaimedTicket("num-1", 1, "own-1", asyncio.Event())
    assert await v2.worker._dispatch("L1", None, held=on_old) is True
    monkeypatch.setattr(W, "_DIAL_MEMO", W.TTLMemo(ttl_s=10))  # the other pod
    v2.h.number = plivo("num-2")
    on_new = W.ClaimedTicket("num-2", 1, "own-2", asyncio.Event())
    assert await W.Worker(worker_uuid="w-2")._dispatch("L1", None, held=on_new) is False
    assert len(v2.h.call_recorder.calls) == 1
    assert _gave_back(v2) == [("give_back", ("num-2", "L1", 1), {})]


@pytest.mark.parametrize("exit_path", ["hours_closed", "blacklisted", "no_config"])
async def test_a_reaped_dispatch_leaves_the_lead_to_its_new_holder(v2, exit_path):
    # The reaper freed this ticket's line (180 s after its claim), unlocked the
    # lead and re-issued it, and a newer ticket's coroutine locked it. The old one's exit
    # finds its lease gone (return_line: -1) and must not unlock, defer, finish or
    # re-queue the lead it no longer owns.
    async def _not_ours(*a, **k):
        v2.order.append(("give_back", a[:3], {}))
        return -1

    v2.return_line.side_effect = _not_ours
    if exit_path == "hours_closed":
        v2.h.within_hours = False
    elif exit_path == "blacklisted":
        v2.h.is_blacklisted = True
    else:
        v2.h.config = None  # NO_CONFIG: finished
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {})]
    assert v2.h.deferred == []
    assert v2.h.completions == []
    assert "L1" not in v2.h.released_locks
    assert [o for o in v2.order if o[0] == "schedule"] == []


async def test_a_sid_less_reply_on_a_prewritten_dial_row_is_held_not_reverted(v2):
    # A reply without a call id may have rung. On a dial row written before
    # the request (Plivo) that row is already the hold: it must not go back to BACKLOG
    # (a redial) and the line stays with the call that may exist.
    from app.schemas import CallProvider

    v2.h.number.provider = CallProvider.PLIVO
    v2.h.call_recorder.dial_replies = [{"status": "call_initiated", "sid": None}]
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.h.leads["L1"].status == LeadCallStatus.PROCESSING  # the hold
    assert _gave_back(v2) == []
    assert [o for o in v2.order if o[0] == "schedule"] == []


async def test_defer_gives_the_line_back_before_requeue(v2, monkeypatch):
    # race A: enqueue skips a lead that still holds a line
    v2.h.within_hours = False

    async def _defer(lead_id, secs):
        return NS(next_attempt_at=datetime.now(timezone.utc))

    monkeypatch.setattr(W, "defer_lead_next_attempt_and_release_lock", _defer)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert [o[0] for o in v2.order] == ["give_back", "schedule"]
    assert v2.order[1][2] == "tmpl-1"


async def test_paused_reseller_gives_back_before_requeue(v2, monkeypatch):
    monkeypatch.setattr(v2.worker, "_reseller_paused", AsyncMock(return_value=True))
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert [o[0] for o in v2.order] == ["give_back", "schedule"]


async def test_lost_lease_means_no_dial(v2, monkeypatch):
    # race D: our ticket was reaped while the checks ran
    v2.mark.return_value = W.v2_scripts.Mark.REFUSED
    unrecord = AsyncMock()
    monkeypatch.setattr(v2.worker, "_unrecord_call_limit", unrecord)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.h.call_recorder.calls == []
    unrecord.assert_awaited_once()
    assert [o[0] for o in v2.order] == ["give_back", "schedule"]
    assert "L1" in v2.h.released_locks


async def test_a_superseded_ticket_leaves_the_lead_to_its_new_holder(v2, monkeypatch):
    # The reaper freed our line, unlocked the lead and re-issued it; the
    # newer ticket's coroutine may hold the lock now: no unlock, no re-queue from us
    v2.mark.return_value = W.v2_scripts.Mark.SUPERSEDED
    unrecord = AsyncMock()
    monkeypatch.setattr(v2.worker, "_unrecord_call_limit", unrecord)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.h.call_recorder.calls == []
    unrecord.assert_awaited_once()
    assert "L1" not in v2.h.released_locks
    assert [o[0] for o in v2.order] == ["give_back"]  # the no-op net in _dispatch


async def test_lock_failure_gives_back_then_requeues_30s_if_still_backlog(v2):
    v2.h.locked_lead_ids.add("L1")  # a stale ticket's task holds the lock
    before = datetime.now(timezone.utc)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert [o[0] for o in v2.order] == ["give_back", "schedule"]
    assert 25 < (v2.order[1][3] - before).total_seconds() < 40


async def test_lock_failure_requeue_is_never_before_the_db_next_attempt(v2):
    v2.h.locked_lead_ids.add("L1")
    v2.lead.next_attempt_at = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.order[1][3] == v2.lead.next_attempt_at


async def test_not_placed_give_back_is_retried_once_on_a_redis_error(v2):
    v2.h.call_recorder.make_call = lambda *a, **k: None  # type: ignore[assignment]
    v2.return_line.side_effect = [None, 0]
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.return_line.await_count == 2
    assert all(c.kwargs["not_placed"] for c in v2.return_line.await_args_list)


async def test_a_lead_no_longer_backlog_when_read_is_dropped_and_its_line_given_back(
    v2,
):
    v2.lead.status = LeadCallStatus.PROCESSING
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert [o[0] for o in v2.order] == ["give_back"]


@pytest.mark.parametrize("status", [LeadCallStatus.FINISHED, LeadCallStatus.PROCESSING])
async def test_a_lock_refused_on_a_lead_no_longer_backlog_drops_the_ticket(
    v2, monkeypatch, status
):
    # The lead finished fast (or another dispatch took it) between this dispatch's read
    # and its lock, e.g. after a stale backlog page re-added it: the line goes back and
    # the ticket is dropped. Only a lead still BACKLOG is re-queued (rule 25), so this
    # one is never bounced back every 30 s.
    real = v2.h.acquire_lock_on_lead_by_id

    async def read(lead_id):
        return copy.copy(v2.h.leads.get(lead_id))  # a row as read, not the live one

    async def lock(lead_id, expected_status):
        v2.lead.status = status
        return await real(lead_id, expected_status)

    monkeypatch.setattr(W, "get_lead_by_id", read)
    monkeypatch.setattr(W, "acquire_lock_on_lead_by_id", lock)
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert [o[0] for o in v2.order] == ["give_back"]


async def test_provider_none_means_not_placed(v2):
    v2.h.call_recorder.make_call = lambda *a, **k: None  # type: ignore[assignment]
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {"not_placed": True})]


async def test_provider_error_means_not_placed(v2):
    v2.h.call_recorder._raise_exc = RuntimeError("provider down")
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {"not_placed": True})]


async def test_sidless_reply_keeps_the_line_held(v2):
    # Exotel's empty 2xx may have rung: the line is not given back (nor force-returned)
    v2.h.call_recorder._sid = None
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert _gave_back(v2) == []


async def test_status_cas_lost_after_dial_keeps_the_line_with_the_call(v2):
    v2.h.cas_succeeds = False
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    assert len(v2.h.call_recorder.calls) == 1
    assert _gave_back(v2) == []


async def test_legacy_path_unchanged(v2, fake_redis):
    v2.acquire_token.side_effect = None
    v2.acquire_token.return_value = "tok"
    v2.acquire_db.return_value = True
    assert await v2.worker._dispatch("L1", None) is True
    v2.acquire_token.assert_awaited_once()
    v2.mark.assert_not_awaited()
    assert _gave_back(v2) == []


async def test_give_back_names_its_owner(v2):
    v2.h.is_blacklisted = True
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    assert v2.return_line.await_args.args == ("num-1", "L1", 1, "own-1")


def _refs(dials) -> set:
    return {json.dumps(d["dial_ref"], sort_keys=True) for d in dials}


async def test_a_held_line_asks_for_429s(v2):
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    assert len(v2.h.call_recorder.dials) == 1  # make_call got report_throttle=True
    assert len(v2.h.call_recorder.calls) == 1


async def test_a_throttled_dial_is_sent_again_with_the_same_dial_ref(v2):
    rec = v2.h.call_recorder
    rec.dial_replies = [THROTTLED, THROTTLED]
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    assert len(rec.dials) == 3 and len(_refs(rec.dials)) == 1
    assert len(rec.calls) == 1
    assert _gave_back(v2) == []  # the line belongs to the call


async def test_throttled_dial_re_posts_with_the_same_dial_ref_then_gives_up_once(
    v2, monkeypatch
):
    async def _defer(lead_id, secs):
        return NS(next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=secs))

    monkeypatch.setattr(W, "defer_lead_next_attempt_and_release_lock", _defer)
    rec = v2.h.call_recorder
    rec.dial_replies = [THROTTLED] * 1000
    assert await v2.worker._dispatch("L1", None, held=HELD) is False
    # re-sent with doubling waits for the whole 60 s budget, always the same request
    assert rec.calls == [] and 3 <= len(rec.dials) <= 16 and len(_refs(rec.dials)) == 1
    # today's not-placed path, once: one give-back, one deferral, one mark, and no DB
    # write per 429 (Review Focus 2)
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {"not_placed": True})]
    assert [o[0] for o in v2.order].count("schedule") == 1
    assert v2.mark.await_count == 1


async def test_a_stopping_pod_stops_re_sending_a_throttled_dial(v2):
    rec = v2.h.call_recorder
    rec.dial_replies = [THROTTLED] * 1000
    stopping = asyncio.Event()
    stopping.set()
    held = W.ClaimedTicket("num-1", 1, "own-1", stopping)
    assert await v2.worker._dispatch("L1", None, held=held) is False
    assert len(rec.dials) == 1 and rec.calls == []
    assert _gave_back(v2) == [("give_back", ("num-1", "L1", 1), {"not_placed": True})]


async def test_a_slow_greeting_prewarm_does_not_hold_the_dial(v2, monkeypatch):
    started, release, cancelled = asyncio.Event(), asyncio.Event(), []

    async def slow(**kw):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    monkeypatch.setattr(W, "get_template_by_id", AsyncMock(return_value=TEMPLATE))
    monkeypatch.setattr(W, "_prewarm_initial_greeting_with_retry", slow)
    monkeypatch.setattr(W, "BB_V2_PREWARM_WAIT_S", 0.01)
    dispatch = v2.worker._dispatch("L1", None, held=HELD)
    assert await asyncio.wait_for(dispatch, timeout=5) is True
    assert started.is_set() and len(v2.h.call_recorder.calls) == 1
    await asyncio.sleep(0)
    assert cancelled == []  # still running in the background, through the ring
    release.set()


async def test_a_quick_greeting_prewarm_is_ready_before_the_dial(v2, monkeypatch):
    order = []

    async def quick(**kw):
        order.append("prewarm")

    def dial(to, from_number, **kw):
        order.append("dial")
        return {"sid": "CA-1"}

    monkeypatch.setattr(W, "get_template_by_id", AsyncMock(return_value=TEMPLATE))
    monkeypatch.setattr(W, "_prewarm_initial_greeting_with_retry", quick)
    monkeypatch.setattr(v2.h.call_recorder, "make_call", dial)
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    assert order == ["prewarm", "dial"]


async def test_no_free_tts_slot_skips_the_prewarm(v2, monkeypatch):
    prewarm = AsyncMock()
    monkeypatch.setattr(W, "get_template_by_id", AsyncMock(return_value=TEMPLATE))
    monkeypatch.setattr(W, "_prewarm_initial_greeting_with_retry", prewarm)
    monkeypatch.setattr(W, "_TTS_SLOTS", asyncio.Semaphore(0))  # every slot taken
    monkeypatch.setattr(W, "_GREETING_PREWARM_TIMEOUT_S", 0.01)
    monkeypatch.setattr(W, "BB_V2_PREWARM_WAIT_S", 0.05)
    assert await v2.worker._dispatch("L1", None, held=HELD) is True  # fail-open
    prewarm.assert_not_awaited()


async def test_a_prewarm_gives_its_tts_slot_back(v2, monkeypatch):
    prewarm = AsyncMock()
    monkeypatch.setattr(W, "get_template_by_id", AsyncMock(return_value=TEMPLATE))
    monkeypatch.setattr(W, "_prewarm_initial_greeting_with_retry", prewarm)
    monkeypatch.setattr(W, "_TTS_SLOTS", asyncio.Semaphore(1))  # one slot per pod
    monkeypatch.setattr(W, "_GREETING_PREWARM_TIMEOUT_S", 0.5)
    v2.h.add_lead(make_lead("L2"))
    assert await v2.worker._dispatch("L1", None, held=HELD) is True
    held = W.ClaimedTicket("num-1", 2, "own-2", asyncio.Event())
    assert await W.Worker()._dispatch("L2", None, held=held) is True
    assert prewarm.await_count == 2  # the second found the slot free again
