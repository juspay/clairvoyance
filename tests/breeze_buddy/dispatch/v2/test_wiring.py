"""T12 wiring: today's worker hands v2-owned leads back (rule 21), cancel reaches v2 rooms,
and the app starts and stops the v2 acceptor and sweep with today's dispatcher."""

import inspect
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import (
    queue as queue_mod,
    worker as worker_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET, channel_key
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import release as R
from app.schemas import CallDirection
from tests.breeze_buddy.dispatch.conftest import make_lead

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def v2(monkeypatch, harness, fake_redis):
    """v2 in use; the lead's number mode and its room enqueue are controlled per test."""
    harness.add_lead(make_lead("L1"))
    fake_redis.client.lists[channel_key("num-1")] = ["tok-1"]
    # the dial path's CRM mirror needs a DB; not under test here
    monkeypatch.setattr(
        worker_mod, "spawn_background_task", lambda coro, name=None: coro.close()
    )
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    mode = AsyncMock(return_value="v2")
    room = AsyncMock(return_value=True)
    invalidate = AsyncMock()
    monkeypatch.setattr(queue_mod, "_number_mode_or_none", mode)
    # Redis still holds v2's state (fix 1-B's hold is tested in test_break_pC_wiring)
    monkeypatch.setattr(
        queue_mod, "_epoch_present_or_none", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(queue_mod, "_schedule_v2", room)
    monkeypatch.setattr(queue_mod, "_invalidate_route", invalidate)
    return harness, mode, room, invalidate


async def _dispatch() -> bool:
    return await worker_mod.Worker(worker_uuid="w-test")._dispatch("L1", None)


# -- legacy redirect ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["v2_pending", "v2", "draining"])
async def test_lead_on_a_v2_accounted_number_goes_back_to_its_room(
    v2, fake_redis, mode
):
    h, mode_read, room, _ = v2
    mode_read.return_value = mode
    assert await _dispatch() is False
    assert h.call_recorder.calls == []  # no dial
    mode_read.assert_awaited_once_with("num-1")  # the number today's rule picked
    lead = h.leads["L1"]
    room.assert_awaited_once_with("L1", lead.next_attempt_at, "tmpl-1")  # due time kept
    assert h.released_locks == ["L1"] and h.deferred == []
    assert fake_redis.client.lists[channel_key("num-1")] == ["tok-1"]  # no token taken


async def test_unreadable_mode_defers_and_never_dials(v2, fake_redis):
    h, mode_read, room, _ = v2
    mode_read.return_value = None
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]
    room.assert_not_awaited()


async def test_legacy_number_takes_todays_path(v2):
    h, mode_read, room, _ = v2
    mode_read.return_value = "legacy"
    assert await _dispatch() is True
    assert len(h.call_recorder.calls) == 1
    room.assert_not_awaited()


async def test_lead_with_no_room_is_deferred_not_bounced_in_a_loop(v2):
    # the route can't be resolved (or stays on today's path): one re-resolve, then defer
    h, _, room, invalidate = v2
    room.return_value = None
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert room.await_count == 2
    invalidate.assert_awaited_once_with("tmpl-1")
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]


async def test_stale_route_is_reresolved_then_the_lead_goes_to_its_room(v2):
    h, _, room, invalidate = v2
    room.side_effect = [None, True]  # route pointed at today's path; re-resolved to v2
    assert await _dispatch() is False
    invalidate.assert_awaited_once_with("tmpl-1")
    assert h.deferred == [] and h.call_recorder.calls == []


async def test_v2_never_used_reads_no_mode(v2, monkeypatch):
    h, mode_read, _, _ = v2
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    epoch = AsyncMock(return_value=False)
    monkeypatch.setattr(queue_mod, "_epoch_present_or_none", epoch)
    assert await _dispatch() is True
    mode_read.assert_not_awaited()
    epoch.assert_not_awaited()


# -- releases while a number is switching on (ruling C-concern 2) ------------------------


@pytest.mark.parametrize("direction", [CallDirection.INBOUND, CallDirection.OUTBOUND])
async def test_a_call_ending_in_v2_pending_releases_through_todays_path(
    monkeypatch, direction
):
    # today's DB gate counts the number's calls while pending (inbound admits there), so
    # both directions release through today's path (Fable M3)
    monkeypatch.setattr(R, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2_pending"))
    release = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr(R.scripts, "release", release)
    lead = NS(id="L1", telephony_number_id="N1", call_direction=direction, call_id="C1")
    assert await R.release_lead_line(lead) is None
    release.assert_not_awaited()


# -- cancel -------------------------------------------------------------------------------


async def test_cancel_removes_the_lead_from_its_v2_room(fake_redis, monkeypatch):
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    lookup = AsyncMock(return_value="T2")
    monkeypatch.setattr(queue_mod, "_lead_template_id", lookup)
    z = fake_redis.client.zsets
    z[SCHEDULE_ZSET] = {"L1": 1.0, "L2": 2.0}
    z["bb:q:T1"] = {"L1": 1.0}
    z["bb:q:T2"] = {"L2": 2.0}
    assert await queue_mod.cancel_scheduled_lead("L1", template_id="T1") is True
    assert await queue_mod.cancel_scheduled_lead("L2") is True  # template looked up
    assert z[SCHEDULE_ZSET] == {} and z["bb:q:T1"] == {} and z["bb:q:T2"] == {}
    lookup.assert_awaited_once_with("L2")


async def test_cancel_reads_nothing_v2_until_v2_is_used(fake_redis, monkeypatch):
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    lookup = AsyncMock(side_effect=AssertionError("looked up"))
    monkeypatch.setattr(queue_mod, "_lead_template_id", lookup)
    fake_redis.client.zsets[SCHEDULE_ZSET] = {"L1": 1.0}
    assert await queue_mod.cancel_scheduled_lead("L1") is True
    assert fake_redis.client.zsets[SCHEDULE_ZSET] == {}


async def test_abort_handlers_pass_the_template_to_cancel():
    from app.api.routers.breeze_buddy.campaigns import handlers as campaigns
    from app.api.routers.breeze_buddy.leads import handlers as leads

    call = re.compile(r"cancel_scheduled_lead\(\s*lead_id,\s*template_id=")
    assert call.search(inspect.getsource(leads))
    assert call.search(inspect.getsource(campaigns))


# -- app wiring ---------------------------------------------------------------------------


async def test_app_starts_v2_with_todays_dispatcher_and_stops_them_together():
    import app.main as main

    src = inspect.getsource(main.lifespan)
    start = src.index("await start_workers()")
    assert src.index("await start_acceptor()", start) > start
    assert src.index("Sweeper()", start) > start
    # the sweep stops first; then v2's acceptor and today's workers drain in
    # parallel, so today's shutdown takes no longer than before v2 (audit #7)
    sweeper_stop = src.index("await _v2_sweeper.stop()")
    together = src.index("stop_acceptor(grace_s=25), stop_workers()")
    assert sweeper_stop < together
    assert "await stop_workers()" not in src[together:]
    # every pod's own v2 Redis client closes with the app's
    assert src.index("await close_v2_redis()") > src.index(
        "await close_redis_connections()"
    )


async def test_app_main_imports_in_a_fresh_interpreter():
    code = (
        "import app.main as m\n"
        "from app.ai.voice.agents.breeze_buddy.dispatch.v2 import sweep, switch, hooks\n"
        "assert m.Sweeper is sweep.Sweeper and m.start_acceptor\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
