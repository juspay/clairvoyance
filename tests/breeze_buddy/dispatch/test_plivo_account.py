"""The dispatcher dials on the account a template names, switched BEFORE it
takes any capacity: a refused row fails the lead, and no exit holds a
channel token or the number."""

from __future__ import annotations

from typing import Any

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch.worker as w
from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused, PlivoAccount
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.schemas import CallProvider, LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead, push_ready

US = PlivoAccount(auth_id="MAUS0000000000000001", auth_token="us-token")


def _resolver(result: Any):
    """The call's resolver, faked: every block resolves to ``result``."""

    class Accounts:
        def __init__(self, *_args: Any) -> None:
            pass

        async def get(self, *_args: Any) -> Any:
            if isinstance(result, Exception):
                raise result
            return result

    return Accounts


async def _dispatch_one(harness, fake_redis, monkeypatch, result: Any, lead_id):
    monkeypatch.setattr(w, "Accounts", _resolver(result))
    harness.number = harness.number.model_copy(update={"provider": CallProvider.PLIVO})
    harness.call_recorder.takes_accounts = True  # a Plivo provider
    lead = make_lead(lead_id)
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await push_ready(fake_redis, lead.id)
    await w.Worker(worker_uuid="w-acct")._iteration(session=None)
    return lead


async def test_the_dial_runs_on_the_numbers_account(harness, fake_redis, monkeypatch):
    await _dispatch_one(harness, fake_redis, monkeypatch, US, "lead-us")
    assert len(harness.call_recorder.calls) == 1
    assert harness.call_recorder.accounts == [US]


async def test_a_failed_account_read_holds_nothing(harness, fake_redis, monkeypatch):
    # the error reaches the worker loop, which logs it; the lead stays in the
    # backlog for the reconciler, and no capacity is taken
    with pytest.raises(RuntimeError, match="db down"):
        await _dispatch_one(
            harness, fake_redis, monkeypatch, RuntimeError("db down"), "lead-read"
        )
    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == []
    assert harness.completions == []


async def test_a_refused_account_fails_the_lead_and_holds_nothing(
    harness, fake_redis, monkeypatch
):
    lead = await _dispatch_one(
        harness, fake_redis, monkeypatch, AccountRefused("inactive"), "lead-ref"
    )
    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == []
    assert harness.deferred == []
    assert len(harness.completions) == 1
    assert harness.completions[0]["outcome"] == "NUMBER_UNAVAILABLE"
    assert lead.status == LeadCallStatus.FINISHED
