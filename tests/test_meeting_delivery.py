"""Delivery runs the template's send_http_request hooks with the transcript."""

from datetime import datetime, timezone
from typing import cast

import aiohttp
import pytest

from app.ai.voice.agents.breeze_buddy.recorder import delivery as delivery_mod
from app.ai.voice.agents.breeze_buddy.recorder.delivery import build_payload, deliver
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingEndReason,
    MeetingSegment,
    MeetingState,
)

NOW = datetime(2026, 10, 21, 14, 2, 11, tzinfo=timezone.utc)


def meeting() -> Meeting:
    return Meeting(
        id="mtg_1",
        reseller_id="breeze",
        merchant_id="NOA",
        lead_id="lead_1",
        state=MeetingState.FINISHED,
        started_at=NOW,
        ended_at=NOW,
        duration_s=2479,
        end_reason=MeetingEndReason.VOICE,
        last_heartbeat_at=NOW,
        stt_provider="soniox",
        delivery_state=DeliveryState.PENDING,
        delivery_attempts=0,
        delivery_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


SEGS = [
    MeetingSegment(
        meeting_id="mtg_1",
        seq=0,
        speaker="1",
        text="Let's start.",
        start_ms=4000,
        end_ms=9000,
    ),
    MeetingSegment(
        meeting_id="mtg_1",
        seq=1,
        speaker=None,
        text="Sure.",
        start_ms=70000,
        end_ms=71000,
    ),
]


def test_payload_has_lines_and_a_readable_text():
    p = build_payload(meeting(), SEGS)
    assert p["meeting_id"] == "mtg_1" and p["line_count"] == 2
    assert p["segments"][0] == {
        "seq": 0,
        "speaker": "1",
        "text": "Let's start.",
        "start_ms": 4000,
        "end_ms": 9000,
    }
    assert (
        p["transcript_text"] == "[00:00:04] Speaker 1: Let's start.\n[00:01:10] Sure."
    )


class FakeExecutor:
    calls: list = []
    status = 200

    def __init__(self, session):
        pass

    async def execute(
        self, config, resolved_fields=None, fire_and_forget=True, on_sse_event=None
    ):
        FakeExecutor.calls.append((config.url, config.body, fire_and_forget))
        return (FakeExecutor.status, "ok")


@pytest.fixture(autouse=True)
def _executor(monkeypatch):
    FakeExecutor.calls = []
    FakeExecutor.status = 200
    monkeypatch.setattr(delivery_mod, "HttpRequestExecutor", FakeExecutor)


def hook(url="https://xyne/t", body=None) -> HookConfig:
    return HookConfig.model_validate(
        {"name": "send_http_request", "http_request": {"url": url, "body": body}}
    )


@pytest.mark.asyncio
async def test_a_hook_without_a_body_gets_the_standard_payload():
    ok, err = await deliver(
        meeting(), SEGS, [hook()], session=cast(aiohttp.ClientSession, object())
    )
    assert ok and err is None
    url, body, waited = FakeExecutor.calls[0]
    assert url == "https://xyne/t" and waited is False
    assert body["meeting_id"] == "mtg_1" and len(body["segments"]) == 2


@pytest.mark.asyncio
async def test_a_hook_with_its_own_body_keeps_it():
    ok, _ = await deliver(
        meeting(),
        SEGS,
        [hook(body={"id": "{meeting_id}"})],
        session=cast(aiohttp.ClientSession, object()),
    )
    assert ok and FakeExecutor.calls[0][1] == {"id": "{meeting_id}"}


@pytest.mark.asyncio
async def test_non_2xx_is_a_failure_with_the_reason():
    FakeExecutor.status = 503
    ok, err = await deliver(
        meeting(), SEGS, [hook()], session=cast(aiohttp.ClientSession, object())
    )
    assert not ok and "HTTP 503" in (err or "")


@pytest.mark.asyncio
async def test_no_http_hook_is_reported_not_silently_sent():
    other = HookConfig.model_validate({"name": "update_outcome_in_database"})
    ok, err = await deliver(
        meeting(), SEGS, [other], session=cast(aiohttp.ClientSession, object())
    )
    assert not ok and "no send_http_request" in (err or "") and FakeExecutor.calls == []
