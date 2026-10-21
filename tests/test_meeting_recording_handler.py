"""start_meeting_recording builtin and the stop follow-through, with a fake bot."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame

from app.ai.voice.agents.breeze_buddy.handlers.internal import meeting_recording as mr
from app.ai.voice.agents.breeze_buddy.recorder import session as session_mod
from app.ai.voice.agents.breeze_buddy.recorder.session import MeetingConfig
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingEndReason,
    MeetingState,
)

NOW = datetime.now(timezone.utc)


def meeting(meeting_id="mtg_1", state=MeetingState.RECORDING, duration=None) -> Meeting:
    return Meeting(
        id=meeting_id,
        reseller_id="breeze",
        merchant_id="NOA",
        lead_id="lead_1",
        state=state,
        started_at=NOW,
        ended_at=None,
        duration_s=duration,
        end_reason=None,
        last_heartbeat_at=NOW,
        stt_provider="soniox",
        delivery_state=DeliveryState.PENDING,
        delivery_attempts=0,
        delivery_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeGate:
    def __init__(self):
        self.captures = []
        self.stops = 0

    def start_capture(self, **kw):
        self.captures.append(kw)

    def stop_capture(self):
        self.stops += 1


class FakeTask:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame):
        self.frames.append(frame)


def make_context(monkeypatch, enabled=True, hooks=None, stop_phrase=None):
    block = {"enabled": enabled}
    if hooks is not None:
        block["on_stop_hooks"] = hooks
    if stop_phrase:
        block["stop_phrase"] = stop_phrase
    configurations = ConfigurationModel.model_validate(
        {"meeting_recording": block, "stt_configuration": {"provider": "soniox"}}
    )
    events = []

    async def emit(event, payload):
        events.append((event, payload))

    bot = SimpleNamespace(
        speech_gate=FakeGate(),
        configurations=configurations,
        template=None,
        _user_idle_callback_handler=SimpleNamespace(suppress_when=None),
        _stt_service=None,
        _emit_rtvi_event=emit,
        _meeting_session=None,
    )
    lead = SimpleNamespace(
        id="lead_1", reseller_id="breeze", merchant_id="NOA", metaData=None
    )
    ctx: Any = SimpleNamespace(
        bot=bot,
        configurations=configurations,
        lead=lead,
        task=FakeTask(),
        aiohttp_session=object(),
    )

    # storage fakes
    async def no_existing(lead_id):
        return None

    async def create(**kw):
        return meeting(kw["meeting_id"])

    async def finish(meeting_id, reason, final_state=MeetingState.FINISHED):
        return meeting(meeting_id, state=final_state, duration=2479)

    async def append(meeting_id, start_seq, lines):
        return len(lines)

    async def load():
        return MeetingConfig(
            stop_phrases=["hey buddy stop recording the meeting"],
            stop_threshold=0.9,
            max_duration_seconds=3600,
            heartbeat_seconds=3600,
            flush_seconds=3600,
            stt_rotate_seconds=0,
        )

    monkeypatch.setattr(
        session_mod.meetings_db, "get_recording_meeting_by_lead", no_existing
    )
    monkeypatch.setattr(session_mod.meetings_db, "create_meeting", create)
    monkeypatch.setattr(session_mod.meetings_db, "finish_meeting", finish)
    monkeypatch.setattr(session_mod.meetings_db, "append_segments", append)
    monkeypatch.setattr(MeetingConfig, "load", load)
    return cast(TemplateContext, ctx), events


@pytest.mark.asyncio
async def test_start_opens_a_session_and_tells_the_llm_what_to_say(monkeypatch):
    ctx, events = make_context(monkeypatch)
    result = await mr.start_meeting_recording(ctx, {})
    assert result["status"] == "recording_started"
    assert "Starting the recording of your meeting." in result["instruction"]
    assert ctx.bot._meeting_session.is_recording
    assert ctx.bot.speech_gate.captures[0]["stop_phrase"] == [
        "hey buddy stop recording the meeting"
    ]
    assert events == [("recording_started", {"meeting_id": result["meeting_id"]})]

    again = await mr.start_meeting_recording(ctx, {})
    assert again["status"] == "already_recording"
    await ctx.bot._meeting_session.stop(MeetingEndReason.VOICE)


@pytest.mark.asyncio
async def test_template_stop_phrase_overrides_the_dynamic_default(monkeypatch):
    ctx, _ = make_context(monkeypatch, stop_phrase="buddy that's a wrap")
    await mr.start_meeting_recording(ctx, {})
    assert ctx.bot.speech_gate.captures[0]["stop_phrase"] == ["buddy that's a wrap"]
    await ctx.bot._meeting_session.stop(MeetingEndReason.VOICE)


@pytest.mark.asyncio
async def test_disabled_template_refuses(monkeypatch):
    ctx, _ = make_context(monkeypatch, enabled=False)
    assert (await mr.start_meeting_recording(ctx, {}))["status"] == "error"
    assert ctx.bot._meeting_session is None


@pytest.mark.asyncio
async def test_stop_speaks_the_duration_notes_the_lead_and_delivers(monkeypatch):
    ctx, events = make_context(
        monkeypatch,
        hooks=[
            {"name": "send_http_request", "http_request": {"url": "https://xyne/t"}}
        ],
    )
    noted = []

    async def append_metadata_field(lead_id, fields):
        noted.append((lead_id, fields))

    delivered = []

    async def fake_deliver(meeting, segments, hooks, session):
        delivered.append((meeting.id, len(hooks)))
        return True, None

    states = []

    async def set_state(meeting_id, state, error=None):
        states.append((meeting_id, state, error))
        return True

    async def segments(meeting_id):
        return []

    monkeypatch.setattr(mr.lead_db, "append_metadata_field", append_metadata_field)
    monkeypatch.setattr(mr, "deliver", fake_deliver)
    monkeypatch.setattr(mr.meetings_db, "set_delivery_state", set_state)
    monkeypatch.setattr(mr.meetings_db, "get_segments", segments)

    await mr.start_meeting_recording(ctx, {})
    finished = await ctx.bot._meeting_session.stop(MeetingEndReason.VOICE)

    frame = ctx.task.frames[-1]
    assert isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm
    first = cast(Any, frame.messages[0])
    assert "Recorded your meeting for 41 minutes" in str(first["content"])
    assert ctx.lead.metaData["meeting_ids"] == [finished.id]
    assert noted == [("lead_1", {"meeting_ids": [finished.id]})]
    assert delivered == [(finished.id, 1)]
    assert states == [(finished.id, DeliveryState.SENT, None)]
    assert events[-1][0] == "recording_saved"
