"""Voice max-call-duration cap (utils/call_duration.py).

Template: ``max_call_duration_minutes`` defaults to 30, rejects < 5. Timer:
ends a live call as system-ended via end_conversation, keeps an existing
outcome, never fires mid-handoff (human transfer, hold, agent rebuild), and
its finalization survives run()'s teardown. After a Plivo warm transfer the
remaining budget caps the human leg (<Dial timeLimit> / MPC max_duration).
IVR (DTMF) calls cap themselves inside the walker.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Callable, List

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy import agent as agent_mod
from app.ai.voice.agents.breeze_buddy.handlers.internal import (
    hold_and_consult as hold_mod,
    warm_transfer as warm_mod,
)
from app.ai.voice.agents.breeze_buddy.ivr import walker as walker_mod
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import plivo as plivo_mod
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.conference import (
    PlivoConferenceService,
)
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.ai.voice.agents.breeze_buddy.utils import (
    call_duration as cd,
    warm_transfer as wt_utils,
)
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker


def make_agent(outcome: str | None = None) -> agent_mod.Agent:
    agent = agent_mod.Agent(transport_type="telephony")
    agent.call_sid = "CALL-1"
    agent.lead = LeadCallTracker(
        id="lead-1",
        reseller_id="res-1",
        template="t",
        merchant_id="merchant-1",
        status=LeadCallStatus.PROCESSING,
        outcome=outcome,
        call_id="CALL-1",
        payload={},
    )
    return agent


@pytest.fixture
def ended(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Replace end_conversation with a recorder that marks the call ended."""
    calls: List[Any] = []

    async def fake_end_conversation(context: Any, args: Any) -> dict:
        calls.append(context)
        context.bot.conversation_ended = True
        return {}

    monkeypatch.setattr(cd, "end_conversation", fake_end_conversation)
    monkeypatch.setattr(cd, "max_call_duration_seconds", lambda _cfg: 0.01)
    return calls


# ── template config ────────────────────────────────────────────────────────


def test_config_default_and_minimum() -> None:
    assert ConfigurationModel().max_call_duration_minutes == 30
    assert ConfigurationModel(max_call_duration_minutes=5).max_call_duration_minutes
    for bad in (4, 0, None):
        with pytest.raises(ValidationError):
            ConfigurationModel.model_validate({"max_call_duration_minutes": bad})


def test_duration_seconds() -> None:
    cfg = ConfigurationModel(max_call_duration_minutes=12)
    assert cd.max_call_duration_seconds(cfg) == 12 * 60
    assert cd.max_call_duration_seconds(None) == 30 * 60


# ── timer ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prior, expected", [(None, "BUSY"), ("CONFIRMED", "CONFIRMED")]
)
async def test_timer_ends_live_call_as_system(
    ended: List[Any], prior: str | None, expected: str
) -> None:
    agent = make_agent(outcome=prior)

    await cd.enforce_max_call_duration(agent)

    assert len(ended) == 1
    assert agent.lead is not None and agent.lead.outcome == expected
    meta = agent.lead.metaData or {}
    assert meta["call_ended_by"] == "system"
    assert meta["call_end_reason"] == "max_call_duration_exceeded"


async def test_timer_noop_when_call_already_ended(ended: List[Any]) -> None:
    agent = make_agent()
    agent.conversation_ended = True

    await cd.enforce_max_call_duration(agent)

    assert ended == []


def _pending_transfer(agent: Any, on: bool) -> None:
    agent.pending_transfer = object() if on else None


def _between_generations(agent: Any, on: bool) -> None:
    agent._between_generations = on


def _human_handoff(agent: Any, on: bool) -> None:
    agent.handoff_depth = 1 if on else 0


@pytest.mark.parametrize(
    "handoff", [_pending_transfer, _between_generations, _human_handoff]
)
async def test_timer_waits_out_handoff(
    ended: List[Any], handoff: Callable[[Any, bool], None]
) -> None:
    agent = make_agent()
    handoff(agent, True)

    task = asyncio.create_task(cd.enforce_max_call_duration(agent))
    await asyncio.sleep(0.1)
    assert ended == []  # deadline passed, but the handoff defers the end

    handoff(agent, False)
    await asyncio.wait_for(task, timeout=3.0)
    assert len(ended) == 1


async def test_connected_handoff_leaves_timer_noop(ended: List[Any]) -> None:
    """A connected transfer ends the conversation itself; the timer backs off."""
    agent = make_agent()
    with cd.handoff_in_progress(agent):
        task = asyncio.create_task(cd.enforce_max_call_duration(agent))
        await asyncio.sleep(0.05)
        agent.conversation_ended = True
    await asyncio.wait_for(task, timeout=3.0)
    assert ended == []


async def test_finalization_survives_timer_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = make_agent()
    entered, release = asyncio.Event(), asyncio.Event()
    finished: List[bool] = []

    async def slow_end_conversation(context: Any, args: Any) -> dict:
        context.bot.conversation_ended = True
        entered.set()
        await release.wait()  # e.g. the DB completion write
        finished.append(True)
        return {}

    monkeypatch.setattr(cd, "end_conversation", slow_end_conversation)
    monkeypatch.setattr(cd, "max_call_duration_seconds", lambda _cfg: 0.01)
    task = asyncio.create_task(cd.enforce_max_call_duration(agent))
    await asyncio.wait_for(entered.wait(), timeout=3.0)

    task.cancel()  # run()'s finally, racing a pipeline that ended on its own
    await task
    release.set()
    await asyncio.sleep(0.05)
    assert finished == [True]


# ── handoff guard wiring ───────────────────────────────────────────────────


def test_guard_releases_on_error() -> None:
    agent = make_agent()
    with pytest.raises(RuntimeError):
        with cd.handoff_in_progress(agent):
            raise RuntimeError("transfer blew up")
    assert agent.handoff_depth == 0


class _DepthProbe:
    """Bot stand-in: records handoff_depth whenever a handler reads configurations."""

    def __init__(self) -> None:
        self.handoff_depth = 0
        self.seen: List[int] = []

    @property
    def configurations(self) -> Any:
        self.seen.append(self.handoff_depth)
        return None


async def test_transfer_and_hold_handlers_hold_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _DepthProbe()

    async def fake_number(_id: str) -> Any:
        return SimpleNamespace(number="+911111111111")

    monkeypatch.setattr(warm_mod, "get_telephony_number_by_id", fake_number)
    lead = SimpleNamespace(telephony_number_id="num-1", payload={})
    warm_ctx = SimpleNamespace(bot=bot, lead=lead, call_sid="CALL-1")

    await warm_mod.connect_to_live_agent(warm_ctx, {})  # type: ignore[arg-type]
    await hold_mod.hold_and_consult(SimpleNamespace(bot=bot, call_sid="CALL-1"), {})  # type: ignore[arg-type]

    assert bot.seen == [1, 1]  # inside both handlers
    assert bot.handoff_depth == 0


# ── run() loop ─────────────────────────────────────────────────────────────


async def test_run_stops_when_call_ends_during_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = make_agent()
    generations: List[int] = []
    deferred_during_rebuild: List[bool] = []

    async def setup_ok() -> bool:
        return True

    async def fake_generation() -> None:
        generations.append(1)
        if len(generations) == 1:
            agent.pending_transfer = object()  # type: ignore[assignment]

    async def fake_apply_transfer(bot: Any, transfer: Any) -> None:
        deferred_during_rebuild.append(cd._handoff_active(bot))
        bot.conversation_ended = True  # e.g. customer hung up mid-rebuild

    agent._setup_telephony_transport = setup_ok  # type: ignore[method-assign]
    agent._run_generation = fake_generation  # type: ignore[method-assign]
    monkeypatch.setattr(agent_mod, "apply_transfer", fake_apply_transfer)

    await asyncio.wait_for(agent.run(), timeout=5.0)

    assert generations == [1]  # no unmanaged generation after the end
    assert deferred_during_rebuild == [True]
    assert agent._max_duration_task is not None
    assert agent._max_duration_task.cancelled() or agent._max_duration_task.done()


# ── warm transfer: the rest of the budget caps the human leg (Plivo) ───────


def test_transfer_time_limit_clamps() -> None:
    now = time.time()
    assert wt_utils.transfer_time_limit(None, floor=60) is None
    assert 590 <= (wt_utils.transfer_time_limit(now + 600, floor=60) or 0) <= 600
    assert wt_utils.transfer_time_limit(now - 10, floor=60) == 60  # past deadline
    assert wt_utils.transfer_time_limit(now + 10**6, floor=60) == 14400


async def test_timer_records_deadline(ended: List[Any]) -> None:
    agent = make_agent()
    before = time.time()

    await cd.enforce_max_call_duration(agent)

    assert agent.max_call_end_at is not None
    assert before <= agent.max_call_end_at <= time.time() + 1


async def test_transfer_flag_stores_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: List[str] = []

    class FakeRedis:
        async def setex(self, key: str, value: str, ttl: int) -> bool:
            stored.append(value)
            return True

    async def fake_redis() -> FakeRedis:
        return FakeRedis()

    monkeypatch.setattr(wt_utils, "get_redis_service", fake_redis)
    await wt_utils.set_transfer_flag("CALL-1", "r", "m", max_call_end_at=123.0)

    assert json.loads(stored[0])["max_call_end_at"] == 123.0


@pytest.mark.parametrize("end_in, expected", [(600, True), (None, False)])
async def test_plivo_dial_xml_time_limit(end_in: int | None, expected: bool) -> None:
    end_at = time.time() + end_in if end_in else None
    flag = {"transfer_number": "+910000000000", "max_call_end_at": end_at}

    xml = bytes((await plivo_mod.plivo_dial_xml(flag, "CALL-1", {})).body).decode()

    assert ('timeLimit="' in xml) is expected  # no deadline → no attribute


async def test_mpc_agent_participant_gets_max_duration() -> None:
    calls: List[dict] = []
    client = SimpleNamespace(
        multi_party_calls=SimpleNamespace(add_participant=lambda **kw: calls.append(kw))
    )
    svc = PlivoConferenceService(client)  # type: ignore[arg-type]

    await svc._add_agent_to_mpc("mpc", "+91a", "+91b", "CALL-1", max_duration=900)
    await svc._add_agent_to_mpc("mpc", "+91a", "+91b", "CALL-1")

    assert calls[0]["max_duration"] == 900
    assert "max_duration" not in calls[1]  # no deadline → Plivo default


def _transfer_ctx(end_at: float) -> Any:
    return SimpleNamespace(
        call_sid="CALL-1",
        provider="plivo",
        lead=SimpleNamespace(reseller_id="r", merchant_id="m"),
        bot=SimpleNamespace(max_call_end_at=end_at),
    )


async def test_transfer_handlers_pass_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags: List[dict] = []
    mpc: List[dict] = []

    async def fake_flag(**kw: Any) -> bool:
        flags.append(kw)
        return True

    async def noop(*_a: Any, **_kw: Any) -> Any:
        return None

    async def failed_transfer(**kw: Any) -> dict:
        mpc.append(kw)
        return {"success": False, "reason": "test"}

    for name in ("set_transfer_flag", "mute_stt", "unmute_stt", "subscribe_and_wait"):
        monkeypatch.setattr(
            warm_mod, name, fake_flag if name == "set_transfer_flag" else noop
        )
    service = SimpleNamespace(
        handle_transfer=failed_transfer, handle_mpc_transfer=failed_transfer
    )
    end_at = time.time() + 600
    kwargs: Any = dict(
        conference_service=service,
        agent_phone_number="+91a",
        conference_name="c",
        telephony_number="+91b",
        customer_phone_number=None,
    )

    await warm_mod._transfer_legacy(_transfer_ctx(end_at), **kwargs)
    await warm_mod._transfer_via_mpc(_transfer_ctx(end_at), **kwargs)

    assert [f["max_call_end_at"] for f in flags] == [end_at, end_at]
    assert 590 <= mpc[-1]["max_duration"] <= 600


# ── IVR calls run outside Agent.run()'s timer: the walker caps itself ──────


async def test_ivr_walker_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = make_agent()
    agent.ws = object()  # type: ignore[assignment]
    agent.stream_sid = "stream-1"
    agent.template = SimpleNamespace(flow={})  # type: ignore[assignment]
    walker = walker_mod.IvrWalker(agent)
    finalized: List[str] = []

    async def noop(*_a: Any) -> None:
        return None

    async def endless_menu(_flow: Any) -> str:
        await asyncio.sleep(3600)  # caller keeps navigating menus
        return "agent"

    async def fake_finalize(call_ended_by: str) -> None:
        finalized.append(call_ended_by)

    monkeypatch.setattr(walker_mod, "resolve_voice_config", noop)
    monkeypatch.setattr(
        walker_mod.IvrModeFlow,
        "model_validate",
        lambda _f: SimpleNamespace(nodes={}, initial_node="main"),
    )
    monkeypatch.setattr(walker_mod, "max_call_duration_seconds", lambda _cfg: 0.05)
    monkeypatch.setattr(walker, "_walk", endless_menu)
    monkeypatch.setattr(walker, "_finalize_and_close", fake_finalize)

    await asyncio.wait_for(walker.run(), timeout=3.0)

    assert finalized == ["system"]  # walker's own finalise path ran
    meta = agent.lead.metaData or {}  # type: ignore[union-attr]
    assert meta["call_end_reason"] == "max_call_duration_exceeded"
    assert agent.lead.outcome == "BUSY"  # type: ignore[union-attr]
