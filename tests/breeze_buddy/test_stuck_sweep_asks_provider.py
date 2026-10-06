"""The stuck-call sweep asks the provider before closing a call that may be live.

The sweep releases the line, so closing a call that is still being spoken
frees a live channel (over-dial) and can re-dial a customer mid-conversation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest
from plivo.exceptions import PlivoRestError, ResourceNotFoundError

# Import the dispatch package first (managers.calls <-> dispatch import cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.telephony import (
    utils as telephony_utils,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    plivo as plivo_mod,
)
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus
from tests.breeze_buddy.test_completed_call_reconcile import make_lead


class _FakeProvider:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: List[str] = []
        self.asked_leads: List[str] = []

    async def is_call_live(self, lead: Any) -> Optional[bool]:
        self.asked.append(lead.call_id)
        self.asked_leads.append(lead.id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Sweep:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, leads: list) -> None:
        self.closed: List[str] = []
        self.released: List[str] = []
        self.provider_names: List[CallProvider] = []
        by_id = {lead.id: lead for lead in leads}

        async def stale(*_a: Any, **_k: Any) -> list:
            return leads

        async def acquire(lead_id: str, **_k: Any) -> Any:
            return by_id[lead_id]

        async def update(**kw: Any) -> None:
            self.closed.append(kw["id"])

        async def release(lead: Any) -> None:
            self.released.append(lead.id)

        async def noop(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", stale)
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", acquire)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", update)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", noop)
        monkeypatch.setattr(calls_mod, "_release_call_resources", release)
        monkeypatch.setattr(calls_mod, "_get_lead_config", noop)
        # release's P1 page on every close: a unit test must not reach Redis or Slack
        monkeypatch.setattr(calls_mod, "raise_long_running_call", noop)

    def provider_is(
        self,
        monkeypatch: pytest.MonkeyPatch,
        provider: CallProvider,
        fake: _FakeProvider,
    ) -> None:
        async def number(_id: str) -> TelephonyNumber:
            return TelephonyNumber(
                id="n1",
                number="+910000000000",
                provider=provider,
                status=TelephonyNumberStatus.AVAILABLE,
            )

        def factory(name: CallProvider, *_a: Any, **_k: Any) -> _FakeProvider:
            self.provider_names.append(name)
            return fake

        monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", number)
        monkeypatch.setattr(telephony_utils, "get_voice_provider", factory)


def _lead(lead_id: str = "lead-1", number_id: Optional[str] = "n1") -> Any:
    lead = make_lead(lead_id=lead_id)
    lead.telephony_number_id = number_id
    lead.call_id = f"call-{lead_id}"
    lead.call_initiated_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    return lead


@pytest.mark.asyncio
async def test_live_call_is_not_closed_and_keeps_its_line(monkeypatch) -> None:
    sweep = _Sweep(monkeypatch, [_lead()])
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, _FakeProvider(True))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.closed == [] and sweep.released == []
    assert sweep.provider_names == [CallProvider.PLIVO]


@pytest.mark.parametrize("answer", [False, None])
@pytest.mark.asyncio
async def test_ended_or_unknown_call_is_closed_as_before(monkeypatch, answer) -> None:
    sweep = _Sweep(monkeypatch, [_lead()])
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, _FakeProvider(answer))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.closed == ["lead-1"] and sweep.released == ["lead-1"]
    assert sweep.provider_names == [CallProvider.PLIVO]


@pytest.mark.asyncio
async def test_lookup_error_skips_the_lead(monkeypatch) -> None:
    sweep = _Sweep(monkeypatch, [_lead()])
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, _FakeProvider(RuntimeError("x")))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.closed == [] and sweep.released == []


@pytest.mark.asyncio
async def test_lookup_timeout_skips_the_lead(monkeypatch) -> None:
    import asyncio

    class _Slow(_FakeProvider):
        async def is_call_live(self, lead: Any) -> Optional[bool]:
            await asyncio.sleep(5)
            return False

    monkeypatch.setattr(calls_mod, "BB_STUCK_SWEEP_LOOKUP_TIMEOUT_S", 0.01)
    sweep = _Sweep(monkeypatch, [_lead()])
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, _Slow(False))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.closed == []


@pytest.mark.asyncio
async def test_lead_without_call_id_is_closed_without_asking(monkeypatch) -> None:
    lead = _lead()
    lead.call_id = None
    sweep = _Sweep(monkeypatch, [lead])
    fake = _FakeProvider(True)
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, fake)

    await calls_mod.reconcile_stuck_processing_leads()

    assert fake.asked == [] and sweep.closed == ["lead-1"]


@pytest.mark.asyncio
async def test_lead_without_a_number_is_closed_as_before(monkeypatch) -> None:
    sweep = _Sweep(monkeypatch, [_lead(number_id=None)])
    fake = _FakeProvider(True)
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, fake)

    await calls_mod.reconcile_stuck_processing_leads()

    assert fake.asked == [] and sweep.closed == ["lead-1"]


@pytest.mark.asyncio
async def test_provider_without_lookup_is_closed_as_before(monkeypatch) -> None:
    """Twilio and the rest inherit the base default: unknown."""

    class _Twilioish(VoiceCallProvider):
        async def handle_websocket(self, websocket, provider):  # pragma: no cover
            return None

        def make_call(self, *a, **k):  # pragma: no cover
            return None

    assert await _Twilioish(None, None).is_call_live(_lead()) is None

    sweep = _Sweep(monkeypatch, [_lead()])
    sweep.provider_is(monkeypatch, CallProvider.TWILIO, _Twilioish(None, None))  # type: ignore[arg-type]

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.closed == ["lead-1"]


@pytest.mark.asyncio
async def test_per_run_cap_skips_the_rest(monkeypatch) -> None:
    monkeypatch.setattr(calls_mod, "BB_STUCK_SWEEP_MAX_LOOKUPS", 2)
    leads = [_lead(f"l{i}") for i in range(4)]
    sweep = _Sweep(monkeypatch, leads)
    fake = _FakeProvider(False)
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, fake)

    await calls_mod.reconcile_stuck_processing_leads()

    assert len(fake.asked) == 2
    assert len(sweep.closed) == 2
    assert set(sweep.closed) == set(fake.asked_leads)


@pytest.mark.asyncio
async def test_run_deadline_stops_lookups(monkeypatch) -> None:
    monkeypatch.setattr(calls_mod, "BB_STUCK_SWEEP_LOOKUP_DEADLINE_S", -1)
    sweep = _Sweep(monkeypatch, [_lead()])
    fake = _FakeProvider(False)
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, fake)

    await calls_mod.reconcile_stuck_processing_leads()

    assert fake.asked == [] and sweep.closed == []


@pytest.mark.asyncio
async def test_call_older_than_the_ceiling_is_closed_without_asking(
    monkeypatch,
) -> None:
    """A lookup that never clears (e.g. a refused account) must not hold the
    line forever."""
    lead = _lead()
    lead.call_initiated_time = datetime.now(timezone.utc) - timedelta(minutes=241)
    sweep = _Sweep(monkeypatch, [lead])
    fake = _FakeProvider(RuntimeError("account refused"))
    sweep.provider_is(monkeypatch, CallProvider.PLIVO, fake)

    await calls_mod.reconcile_stuck_processing_leads()

    assert fake.asked == []
    assert sweep.closed == ["lead-1"] and sweep.released == ["lead-1"]


# --- the Plivo lookup itself -------------------------------------------------


class _LiveCalls:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.seen: dict = {}
        self.asked: List[str] = []

    def get(self, call_uuid: str) -> Any:
        self.asked.append(call_uuid)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _plivo(monkeypatch, outcome: Any) -> tuple:
    async def account(_lead: Any) -> None:
        return None

    monkeypatch.setattr(plivo_mod, "lead_plivo_account", account)
    provider = plivo_mod.PlivoProvider.__new__(plivo_mod.PlivoProvider)  # no creds
    live = _LiveCalls(outcome)
    provider.PLIVO_AUTH_ID = provider.PLIVO_AUTH_TOKEN = "x"
    provider._use = lambda _account: None  # type: ignore[method-assign]

    def client(_id: str, _token: str, timeout: float) -> Any:
        live.seen["timeout"] = timeout
        return type("C", (), {"live_calls": live})()

    monkeypatch.setattr(plivo_mod.plivo, "RestClient", client)
    return provider, live


@pytest.mark.asyncio
async def test_plivo_live_call_is_live(monkeypatch) -> None:
    provider, live = _plivo(monkeypatch, object())
    assert await provider.is_call_live(_lead()) is True
    assert live.asked == ["call-lead-1"]
    assert live.seen["timeout"] == plivo_mod.BB_STUCK_SWEEP_LOOKUP_TIMEOUT_S


@pytest.mark.asyncio
async def test_plivo_not_found_is_ended(monkeypatch) -> None:
    provider, _ = _plivo(monkeypatch, ResourceNotFoundError("gone"))
    assert await provider.is_call_live(_lead()) is False


@pytest.mark.asyncio
async def test_plivo_other_error_propagates(monkeypatch) -> None:
    provider, _ = _plivo(monkeypatch, PlivoRestError("boom"))
    with pytest.raises(PlivoRestError):
        await provider.is_call_live(_lead())
