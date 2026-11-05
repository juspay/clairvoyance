"""A Plivo dial's row is written before the request.

PROCESSING, no call id, ``call_initiated_time = dial_at`` and the unknown-dial
marker, so a webhook that beats Plivo's reply (an answer or hangup inside the
read timeout, or after a slow reply) claims the lead by its dial_ref. The reply
then only stamps the call id; "not placed" reverts the row to BACKLOG.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import READY_LIST, SCHEDULE_ZSET
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_UNKNOWN,
    UNKNOWN_DIAL_META_KEY,
)
from app.schemas import CallProvider, LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead

pytestmark = pytest.mark.asyncio

SID = "CA-plivo-1"


def _webhook_claims(harness: Any, lead_id: str, dial_ref: Dict[str, str]) -> bool:
    """What claim_unknown_dial does in SQL: PROCESSING, no call id, this dial."""
    lead = harness.leads[lead_id]
    if (
        lead.status != LeadCallStatus.PROCESSING
        or lead.call_id
        or lead.call_initiated_time != datetime.fromisoformat(dial_ref["dial_at"])
    ):
        return False
    lead.call_id = SID
    return True


async def _dial(harness: Any, fake_redis: Any, monkeypatch: Any, provider_does):
    """Dispatch one lead on a Plivo number; ``provider_does(lead, dial_ref)`` is
    Plivo's side (webhooks during the dial) and returns make_call's reply."""
    harness.number.provider = CallProvider.PLIVO
    lead = make_lead("lead-p")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)
    seen: List[Any] = []

    async def _make_call(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        dial_ref = kwargs["dial_ref"]
        row = harness.leads[lead.id]
        seen.append(
            (row.status, row.call_id, row.call_initiated_time, dict(row.metaData or {}))
        )
        return provider_does(row, dial_ref)

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _make_call)
    await w.Worker(worker_uuid="w-row-first")._iteration(session=None)
    return lead, seen


async def test_the_row_is_written_before_the_request_and_stamped_after(
    harness, fake_redis, monkeypatch
):
    lead, seen = await _dial(
        harness, fake_redis, monkeypatch, lambda row, ref: {"sid": SID}
    )
    ((status, call_id, initiated, meta),) = seen
    assert status == LeadCallStatus.PROCESSING and call_id is None
    assert UNKNOWN_DIAL_META_KEY in meta
    assert lead.call_id == SID and lead.status == LeadCallStatus.PROCESSING
    assert lead.call_initiated_time == initiated
    assert UNKNOWN_DIAL_META_KEY not in (lead.metaData or {})
    assert await channel_tokens_available(harness.number.id) == 0  # the call's
    assert harness.released_numbers == []


async def test_a_webhook_during_the_dial_claims_the_lead(
    harness, fake_redis, monkeypatch
):
    claimed: List[bool] = []

    def _plivo(row, ref):
        claimed.append(_webhook_claims(harness, row.id, ref))  # answer arrives first
        return {"sid": SID}

    lead, _ = await _dial(harness, fake_redis, monkeypatch, _plivo)
    assert claimed == [True]
    assert lead.call_id == SID and lead.status == LeadCallStatus.PROCESSING
    assert harness.released_numbers == []
    assert lead.id not in harness.released_locks  # the call's webhooks own it


async def test_a_call_that_ended_before_the_reply_is_not_released_again(
    harness, fake_redis, monkeypatch
):
    def _plivo(row, ref):
        assert _webhook_claims(harness, row.id, ref)
        row.status = LeadCallStatus.FINISHED  # its end webhook finished it
        return {"sid": SID}

    lead, _ = await _dial(harness, fake_redis, monkeypatch, _plivo)
    assert lead.status == LeadCallStatus.FINISHED and lead.call_id == SID
    # the end webhook already gave the line back; the worker gives nothing back
    assert harness.released_numbers == []
    assert await channel_tokens_available(harness.number.id) == 0


async def test_not_placed_reverts_the_row_to_backlog(harness, fake_redis, monkeypatch):
    lead, _ = await _dial(harness, fake_redis, monkeypatch, lambda row, ref: None)
    assert lead.status == LeadCallStatus.BACKLOG and lead.call_id is None
    assert UNKNOWN_DIAL_META_KEY not in (lead.metaData or {})
    assert harness.deferred == [(lead.id, 10)]
    assert lead.id not in harness.locked_lead_ids
    assert lead.id in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})  # queued again
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]


async def test_not_placed_but_claimed_keeps_the_line(harness, fake_redis, monkeypatch):
    def _plivo(row, ref):
        assert _webhook_claims(harness, row.id, ref)  # a call exists after all
        return None

    lead, _ = await _dial(harness, fake_redis, monkeypatch, _plivo)
    assert lead.status == LeadCallStatus.PROCESSING and lead.call_id == SID
    assert harness.released_numbers == []
    assert harness.deferred == []


async def test_a_row_that_left_backlog_before_the_dial_is_not_dialled(
    harness, fake_redis, monkeypatch
):
    harness.premark_succeeds = False  # e.g. the merchant finished it
    lead, seen = await _dial(
        harness, fake_redis, monkeypatch, lambda row, ref: {"sid": SID}
    )
    assert seen == []  # make_call never ran
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert lead.id in harness.released_locks


async def test_a_lost_reply_needs_no_second_write(harness, fake_redis, monkeypatch):
    holds: List[Any] = []
    real_hold = harness.hold_unknown_dial

    async def _counting_hold(*args: Any, **kwargs: Any):
        holds.append(args[0])
        return await real_hold(*args, **kwargs)

    monkeypatch.setattr(w, "hold_unknown_dial", _counting_hold)
    lead, _ = await _dial(
        harness,
        fake_redis,
        monkeypatch,
        lambda row, ref: {"status": DIAL_OUTCOME_UNKNOWN, "sid": None},
    )
    assert holds == [lead.id]  # written once, before the request
    assert lead.status == LeadCallStatus.PROCESSING and lead.call_id is None
    assert UNKNOWN_DIAL_META_KEY in (lead.metaData or {})
    assert harness.released_numbers == []
    assert harness.deferred == []
