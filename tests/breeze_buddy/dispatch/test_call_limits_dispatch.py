"""The merchant's per-customer call rule at the dial (ADR 0025 stage 1).

Drives the real ``Worker._iteration`` through the shared ``DispatchHarness``.
The limiter is stubbed at the worker's import site (no Redis): these tests pin
WHERE the worker asks, in what ORDER, and what it does with each answer.
``finish_lead_call_limit_reached`` runs for real with its DB write and webhook
captured.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import READY_LIST
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    CallLimitUnavailable,
    CallLimitVerdict,
)
from app.schemas import ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.merchants import CallLimit
from app.schemas.breeze_buddy.outcomes import (
    CallOutcome,
    ConnectionReason,
    ConnectionStatus,
)
from tests.breeze_buddy.dispatch.conftest import make_lead

RULE = CallLimit(max_calls=2, window_hours=48)


@pytest.fixture
def limiter(harness, monkeypatch):
    """A merchant rule, a stubbed limiter that logs every question in order,
    and the terminal helper's DB write + webhook captured."""
    events = []
    state = SimpleNamespace(
        events=events,
        peek=CallLimitVerdict(allowed=True),
        record=CallLimitVerdict(allowed=True, member="lead:nonce-1"),
        record_raises=None,
        unrecorded=[],
        alerts=[],
        webhooks=[],
    )
    harness.call_limits = (RULE,)

    async def _peek(**kw):
        events.append(("peek", kw["lead_id"]))
        return state.peek

    async def _record(**kw):
        events.append(("record", kw["lead_id"]))
        if state.record_raises:
            raise state.record_raises
        return state.record

    async def _unrecord(**kw):
        state.unrecorded.append(kw["member"])

    async def _alert(error):
        state.alerts.append(error)

    async def _webhook(session, url, data):
        state.webhooks.append((url, data))
        return True

    monkeypatch.setattr(w, "peek_call_limit", _peek)
    monkeypatch.setattr(w, "record_call_limit", _record)
    monkeypatch.setattr(w, "unrecord_call_limit", _unrecord)
    monkeypatch.setattr(w, "raise_call_limit_unavailable", _alert)
    monkeypatch.setattr(
        calls_mod,
        "update_lead_call_completion_details",
        harness.update_lead_call_completion_details,
    )
    monkeypatch.setattr(calls_mod, "send_webhook_with_retry", _webhook)

    make_call = harness.call_recorder.make_call_async

    async def _dial(*args, **kwargs):
        events.append(("make_call", None))
        return await make_call(*args, **kwargs)

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _dial)
    return state


async def _dispatch(harness, fake_redis, lead):
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)
    await w.Worker(worker_uuid="w-cl")._iteration(session=None)


# --------------------------------------------------------------------------
# Who is asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, meta",
    [
        (ExecutionMode.TELEPHONY, None),
        # Merchant-sent fields that still ring the real customer number: they
        # must not switch the cap off (PR #1211 review).
        (ExecutionMode.TELEPHONY_TEST, None),
        (ExecutionMode.TELEPHONY, {"playground": True}),
    ],
)
async def test_every_dial_to_the_customer_is_checked_and_counted(
    harness, fake_redis, limiter, mode, meta
):
    lead = make_lead("lead-1")
    lead.execution_mode = mode
    lead.metaData = meta

    await _dispatch(harness, fake_redis, lead)

    assert limiter.events == [
        ("peek", "lead-1"),
        ("record", "lead-1"),
        ("make_call", None),
    ]


async def test_a_lead_without_a_merchant_has_no_rule_to_ask(
    harness, fake_redis, limiter
):
    lead = make_lead("lead-no-merchant")
    lead.merchant_id = None

    await _dispatch(harness, fake_redis, lead)

    assert harness.call_limit_reads == []
    assert limiter.events == [("make_call", None)]


# --------------------------------------------------------------------------
# When: the record is the last step before the phone rings
# --------------------------------------------------------------------------


async def test_the_record_comes_after_the_greeting_prewarm(
    harness, fake_redis, limiter, monkeypatch
):
    """A worker stopped during the pre-warm (up to ~60s) must never leave an
    entry for a dial that did not happen."""

    async def _template(template_id):
        return SimpleNamespace(id="tmpl-1", is_active=True)

    async def _prewarm(**kwargs):
        limiter.events.append(("prewarm", None))

    monkeypatch.setattr(w, "get_template_by_id", _template)
    monkeypatch.setattr(w, "_prewarm_initial_greeting_with_retry", _prewarm)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert limiter.events == [
        ("peek", "lead-1"),
        ("prewarm", None),
        ("record", "lead-1"),
        ("make_call", None),
    ]


# --------------------------------------------------------------------------
# What each answer does
# --------------------------------------------------------------------------


async def test_a_refused_peek_ends_the_lead_before_any_capacity(
    harness, fake_redis, limiter
):
    limiter.peek = CallLimitVerdict(allowed=False, rule=RULE, count=2)
    lead = make_lead("lead-over")
    lead.payload = {
        **(lead.payload or {}),
        "reporting_webhook_url": "https://m.example/hook",
    }

    await _dispatch(harness, fake_redis, lead)

    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "CALL_LIMIT_REACHED"
    assert [data["outcome"] for _, data in limiter.webhooks] == ["CALL_LIMIT_REACHED"]
    assert harness.deferred == []


async def test_a_refused_record_releases_the_channel_and_ends_the_lead(
    harness, fake_redis, limiter
):
    limiter.record = CallLimitVerdict(allowed=False, rule=RULE, count=2)

    await _dispatch(harness, fake_redis, make_lead("lead-race"))

    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert harness.completions[-1]["outcome"] == "CALL_LIMIT_REACHED"
    # Call outcome columns: never dialled, over the merchant's cap.
    assert harness.completions[-1]["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.NOT_DIALED,
        connection_reason=ConnectionReason.CALL_LIMIT,
    )


async def test_a_call_the_provider_did_not_place_is_taken_back(
    harness, fake_redis, limiter, monkeypatch
):
    """Every adapter maps its own failure (4xx/5xx/429) to None: the phone
    never rang, so the dial must not use up the customer's allowance."""

    async def _refused(*args, **kwargs):
        limiter.events.append(("make_call", None))
        return None

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _refused)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert limiter.unrecorded == ["lead:nonce-1"]
    assert harness.deferred == [("lead-1", 10)]


async def test_a_provider_exception_is_taken_back(harness, fake_redis, limiter):
    harness.call_recorder._raise_exc = RuntimeError("thread died")

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert limiter.unrecorded == ["lead:nonce-1"]
    assert harness.call_recorder.calls == []


async def test_a_reply_without_a_sid_keeps_its_count(
    harness, fake_redis, limiter, monkeypatch
):
    """Exotel's empty 2xx: the call may have rung, so the count stays."""

    async def _no_sid(*args, **kwargs):
        limiter.events.append(("make_call", None))
        return {"status": "success", "message": "Call initiated successfully"}

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _no_sid)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert limiter.unrecorded == []
    assert harness.deferred == [("lead-1", 10)]


async def test_unreadable_rules_defer_without_paging(
    harness, fake_redis, limiter, monkeypatch
):
    """The rules themselves could not be read, so nothing is known about this
    merchant: the dial waits (fail closed) but no alert fires for it."""

    async def _unreadable(merchant_id):
        raise CallLimitUnavailable("column merchants.call_limits does not exist")

    monkeypatch.setattr(w, "merchant_call_limits", _unreadable)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert harness.call_recorder.calls == []
    assert harness.deferred == [("lead-1", w.CALL_LIMIT_UNAVAILABLE_DEFER_S)]
    assert limiter.alerts == []


async def test_a_capped_merchant_whose_rule_is_unreadable_pages(
    harness, fake_redis, limiter, monkeypatch
):
    async def _unreadable(merchant_id):
        raise CallLimitUnavailable("rule unreadable", capped=True)

    monkeypatch.setattr(w, "merchant_call_limits", _unreadable)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert harness.call_recorder.calls == []
    assert harness.deferred == [("lead-1", w.CALL_LIMIT_UNAVAILABLE_DEFER_S)]
    assert limiter.alerts == ["rule unreadable"]


async def test_an_unavailable_record_releases_the_channel_defers_and_alerts(
    harness, fake_redis, limiter
):
    limiter.record_raises = CallLimitUnavailable("redis down", capped=True)

    await _dispatch(harness, fake_redis, make_lead("lead-1"))

    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert harness.deferred == [("lead-1", w.CALL_LIMIT_UNAVAILABLE_DEFER_S)]
    assert limiter.alerts == ["redis down"]


async def test_no_webhook_when_the_terminal_write_does_not_land(
    harness, fake_redis, limiter, monkeypatch
):
    """The lead stays BACKLOG and is refused again on the next dispatch; a
    webhook now would be sent twice (PR #1211 review)."""

    async def _write_fails(**kwargs):
        return None

    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _write_fails)
    limiter.peek = CallLimitVerdict(allowed=False, rule=RULE, count=2)
    lead = make_lead("lead-over")
    lead.payload = {
        **(lead.payload or {}),
        "reporting_webhook_url": "https://m.example/hook",
    }

    await _dispatch(harness, fake_redis, lead)

    assert limiter.webhooks == []
    assert harness.call_recorder.calls == []
