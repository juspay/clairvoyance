"""A stale queue entry never dials a lead before its due time.

A backlog page read before the lead was deferred (or any other stale copy in
Redis) can put a lead back in a queue with an older score than its row's
``next_attempt_at``. The worker re-reads the row when it locks it: if the due
time is still ahead, it re-schedules the lead at that time instead of dialling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import READY_LIST
from app.schemas import LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead

pytestmark = pytest.mark.asyncio


async def _pick(harness, fake_redis, due):
    lead = make_lead("lead-early")
    lead.next_attempt_at = due
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)
    await w.Worker(worker_uuid="w-early")._iteration(session=None)
    return lead


async def test_todays_worker_is_unchanged_for_an_early_copy(harness, fake_redis):
    # today's worker keeps today's behaviour (audit: the guard is v2-only), so a
    # stale early copy on today's path dials exactly as before
    due = datetime.now(timezone.utc) + timedelta(minutes=10)
    lead = await _pick(harness, fake_redis, due)
    assert len(harness.call_recorder.calls) == 1
    assert lead.status == LeadCallStatus.PROCESSING


async def test_a_v2_ticket_for_a_lead_deferred_to_later_is_not_dialled(
    harness, monkeypatch
):
    due = datetime.now(timezone.utc) + timedelta(minutes=10)
    lead = make_lead("lead-early")
    lead.next_attempt_at = due
    harness.add_lead(lead)
    gave_back, scheduled = [], []

    async def _give_back(self):
        gave_back.append(True)

    async def _schedule(lead_id, when, jitter_ms=None, template_id=None):
        scheduled.append((lead_id, when))
        return True

    monkeypatch.setattr(w.Worker, "_give_back_v2", _give_back)
    monkeypatch.setattr(w, "schedule_lead", _schedule)
    worker = w.Worker(worker_uuid="w-early")
    assert (
        await worker._dispatch(
            lead.id, None, held=w.ClaimedTicket("num-1", 1, "own-1", asyncio.Event())
        )
        is False
    )
    assert harness.call_recorder.calls == []
    assert gave_back  # the line went back (the real give-back is idempotent)
    assert scheduled == [(lead.id, due)]
    assert lead.id in harness.released_locks


async def test_a_lead_due_now_is_dialled(harness, fake_redis):
    lead = await _pick(harness, fake_redis, datetime.now(timezone.utc))
    assert len(harness.call_recorder.calls) == 1
    assert lead.status == LeadCallStatus.PROCESSING
