"""start_meeting_recording builtin, the finish follow-through, and wiring."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame

from app.ai.voice.agents.breeze_buddy.handlers.internal import meeting_recording as mr
from app.ai.voice.agents.breeze_buddy.recorder import wiring
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    END_CALL_ENDED,
    END_VOICE,
    MeetingConfig,
    MeetingLine,
    MeetingRecord,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel


class FakeTask:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame):
        self.frames.append(frame)

    async def queue_frames(self, frames):
        self.frames.extend(frames)


def make_bot(monkeypatch, enabled=True, hooks=None, stop_phrase=None, **extra):
    block: dict = {"enabled": enabled, **extra}
    if hooks is not None:
        block["on_stop_hooks"] = hooks
    if stop_phrase:
        block["stop_phrase"] = stop_phrase
    configurations = ConfigurationModel.model_validate({"meeting_recording": block})
    events = []

    async def emit(event, payload):
        events.append((event, payload))

    async def load():
        return MeetingConfig(
            stop_phrases=["hey buddy stop recording the meeting"],
            stop_threshold=0.9,
            max_duration_seconds=3600,
            silence_timeout_seconds=0,
            merge_gap_ms=1500,
        )

    monkeypatch.setattr(MeetingConfig, "load", load)
    bot = SimpleNamespace(
        configurations=configurations,
        template=None,
        conversation_ended=False,
        lead=SimpleNamespace(id="lead_1", reseller_id="breeze", merchant_id="NOA"),
        task=FakeTask(),
        aiohttp_session=object(),
        _emit_rtvi_event=emit,
        _meeting_session=None,
    )
    return bot, events


def ctx(bot) -> TemplateContext:
    return cast(
        TemplateContext,
        SimpleNamespace(
            bot=bot, configurations=bot.configurations, lead=bot.lead, task=bot.task
        ),
    )


@pytest.mark.asyncio
async def test_wiring_builds_attachments_only_for_enabled_templates(monkeypatch):
    off, _ = make_bot(monkeypatch, enabled=False)
    assert await wiring.build_meeting_recording(off) is None

    on, _ = make_bot(monkeypatch, stop_phrase="buddy that's a wrap")
    att = await wiring.build_meeting_recording(on)
    assert att is not None and not att.session.is_recording
    assert att.session.config.stop_phrases == ["buddy that's a wrap"]
    assert (
        att.mute_strategy._session is att.session
        and att.observer._session is att.session
    )


@pytest.mark.asyncio
async def test_start_flips_the_session_and_instructs_the_llm(monkeypatch):
    bot, events = make_bot(monkeypatch)
    att = await wiring.build_meeting_recording(bot)
    assert att is not None
    bot._meeting_session = att.session
    result = await mr.start_meeting_recording(ctx(bot), {})
    assert result["status"] == "recording_started"
    assert "Starting the recording of your meeting." in result["instruction"]
    assert att.session.is_recording and events[0][0] == "recording_started"
    again = await mr.start_meeting_recording(ctx(bot), {})
    assert again["status"] == "already_recording"
    await att.session.stop(END_VOICE)


@pytest.mark.asyncio
async def test_start_refuses_without_a_session(monkeypatch):
    bot, _ = make_bot(monkeypatch, enabled=False)
    assert (await mr.start_meeting_recording(ctx(bot), {}))["status"] == "error"


def record(reason=END_VOICE) -> MeetingRecord:
    now = datetime.now(timezone.utc)
    return MeetingRecord(
        meeting_id="mtg_1",
        lead_id="lead_1",
        merchant_id="NOA",
        reseller_id="breeze",
        started_at=now,
        ended_at=now,
        duration_s=2479,
        end_reason=reason,
        lines=[MeetingLine("1", "hi", 0, 500)],
    )


@pytest.mark.asyncio
async def test_finish_speaks_the_duration_and_delivers_in_the_background(monkeypatch):
    bot, _ = make_bot(
        monkeypatch,
        hooks=[
            {"name": "send_http_request", "http_request": {"url": "https://xyne/t"}}
        ],
    )
    delivered = []

    async def fake_deliver(rec, hooks, session):
        delivered.append((rec.meeting_id, len(hooks)))
        return True, None

    monkeypatch.setattr(mr, "deliver", fake_deliver)
    await mr.on_meeting_finished(bot, record())
    speak, note = bot.task.frames[-2], bot.task.frames[-1]
    assert isinstance(speak, TTSSpeakFrame)
    assert (
        speak.text
        == "Recorded your meeting for 41 minutes. What's next for us to proceed?"
    )
    assert isinstance(note, LLMMessagesAppendFrame) and not note.run_llm
    assert cast(Any, note.messages[0])["role"] == "assistant"
    await bot._meeting_delivery_task
    assert delivered == [("mtg_1", 1)]


@pytest.mark.asyncio
async def test_finish_on_call_end_delivers_inline_and_stays_quiet(monkeypatch):
    bot, _ = make_bot(
        monkeypatch,
        hooks=[
            {"name": "send_http_request", "http_request": {"url": "https://xyne/t"}}
        ],
    )
    delivered = []

    async def fake_deliver(rec, hooks, session):
        delivered.append(rec.end_reason)
        return True, None

    monkeypatch.setattr(mr, "deliver", fake_deliver)
    await mr.on_meeting_finished(bot, record(END_CALL_ENDED))
    assert bot.task.frames == [] and delivered == [END_CALL_ENDED]


def test_spoken_duration_is_grammatical():
    assert mr._spoken_duration(41) == "41 seconds"
    assert mr._spoken_duration(60) == "1 minute"
    assert mr._spoken_duration(1) == "1 second"
    assert mr._spoken_duration(2479) == "41 minutes"


@pytest.mark.asyncio
async def test_template_wording_overrides_the_defaults(monkeypatch):
    bot, _ = make_bot(
        monkeypatch,
        start_confirmation="Recording now.",
        start_instruction="Reply only with: {start_confirmation}",
        stop_confirmation="Done, that was {duration}.",
    )
    att = await wiring.build_meeting_recording(bot)
    assert att is not None
    bot._meeting_session = att.session
    result = await mr.start_meeting_recording(ctx(bot), {})
    assert result["instruction"] == "Reply only with: Recording now."
    await att.session.stop(END_VOICE)
    bot.task.frames.clear()
    await mr.on_meeting_finished(bot, record())
    assert cast(Any, bot.task.frames[-2]).text == "Done, that was 41 minutes."
