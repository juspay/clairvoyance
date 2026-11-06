"""Delivery runs the template's send_http_request hooks with the transcript."""

from datetime import datetime, timezone
from typing import cast

import aiohttp
import pytest

from app.ai.voice.agents.breeze_buddy.recorder import delivery as delivery_mod
from app.ai.voice.agents.breeze_buddy.recorder.delivery import build_payload, deliver
from app.ai.voice.agents.breeze_buddy.recorder.session import MeetingLine, MeetingRecord
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig

NOW = datetime(2026, 10, 21, 14, 2, 11, tzinfo=timezone.utc)


def record() -> MeetingRecord:
    return MeetingRecord(
        meeting_id="mtg_1",
        lead_id="lead_1",
        merchant_id="NOA",
        reseller_id="breeze",
        started_at=NOW,
        ended_at=NOW,
        duration_s=2479,
        end_reason="voice",
        lines=[
            MeetingLine("1", "Let's start.", 4000, 9000),
            MeetingLine(None, "Sure.", 70000, 71000),
        ],
    )


def test_payload_has_lines_and_a_readable_text():
    p = build_payload(record())
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


SESSION = cast(aiohttp.ClientSession, object())


@pytest.mark.asyncio
async def test_a_hook_without_a_body_gets_the_standard_payload():
    ok, err = await deliver(record(), [hook()], SESSION)
    assert ok and err is None
    url, body, waited = FakeExecutor.calls[0]
    assert url == "https://xyne/t" and waited is False and body["meeting_id"] == "mtg_1"


@pytest.mark.asyncio
async def test_non_2xx_is_a_failure_with_the_reason():
    FakeExecutor.status = 503
    ok, err = await deliver(record(), [hook()], SESSION)
    assert not ok and "HTTP 503" in (err or "")


@pytest.mark.asyncio
async def test_no_http_hook_is_reported():
    other = HookConfig.model_validate({"name": "update_outcome_in_database"})
    ok, err = await deliver(record(), [other], SESSION)
    assert not ok and "no send_http_request" in (err or "") and FakeExecutor.calls == []
