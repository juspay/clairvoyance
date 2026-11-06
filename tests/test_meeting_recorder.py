"""Meeting recording, in memory: phrases, session, observer, mute strategy."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pipecat.frames.frames import TranscriptionFrame, UserStartedSpeakingFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from app.ai.voice.agents.breeze_buddy.recorder.mute_strategy import (
    MeetingRecordingMuteStrategy,
)
from app.ai.voice.agents.breeze_buddy.recorder.observer import (
    MeetingRecordingObserver,
)
from app.ai.voice.agents.breeze_buddy.recorder.phrases import (
    is_stop_command,
    normalise,
    split_phrases,
)
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    END_MAX_DURATION,
    END_SILENCE,
    END_VOICE,
    LINES_PER_EVENT,
    MeetingConfig,
    MeetingSession,
)

STOP = [
    "hey buddy stop recording the meeting",
    "hey buddy stop recording",
    "stop recording",
]


def cfg(**over) -> MeetingConfig:
    base = dict(
        stop_phrases=STOP,
        stop_threshold=0.9,
        max_duration_seconds=3600,
        silence_timeout_seconds=0,
        merge_gap_ms=1500,
    )
    base.update(over)
    return MeetingConfig(**base)


def make(**over):
    events, finished = [], []

    async def emit(event, payload):
        events.append((event, payload))

    async def on_finished(record):
        finished.append(record)

    idle = SimpleNamespace(suppress_when=None)
    s = MeetingSession(
        config=cfg(**over),
        lead_id="lead_1",
        merchant_id="NOA",
        reseller_id="breeze",
        idle_handler=idle,
        emit=emit,
        on_finished=on_finished,
    )
    return SimpleNamespace(s=s, events=events, finished=finished, idle=idle)


# ── phrases ──────────────────────────────────────────────────────────────


def test_phrases_normalise_split_and_match_whole_sentences():
    assert normalise("  Hey, Buddy! Stop—recording.  ") == "hey buddy stoprecording"
    assert split_phrases("a | b|| c ") == ["a", "b", "c"]
    assert split_phrases(["x", " ", "y"]) == ["x", "y"]
    assert is_stop_command("Hey buddy, stop recording the meeting.", STOP, 0.9)
    assert is_stop_command("hey body stop recording the meeting", STOP, 0.9)  # STT slip
    assert is_stop_command("Stop recording.", STOP, 0.9)
    assert not is_stop_command(
        "we should stop recording these numbers later", STOP, 0.9
    )
    assert not is_stop_command("Hey, Buddy. Stop.", STOP, 0.9)


# ── session ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_emits_events_and_hands_over_the_record():
    h = make()
    mid = await h.s.start()
    assert mid.startswith("mtg_") and h.s.is_recording
    assert h.idle.suppress_when() is True
    assert h.events[0][0] == "recording_started" and h.events[0][1]["meeting_id"] == mid
    assert await h.s.start() == mid  # idempotent

    h.s.add_line("1", "Let's start.", 4000, 9000)
    h.s.add_line("2", "Sure.", 10000, 15000)
    record = await h.s.stop(END_VOICE)
    assert record is not None and record.end_reason == END_VOICE
    assert [l.text for l in record.lines] == ["Let's start.", "Sure."]
    assert not h.s.is_recording and h.idle.suppress_when is None
    kinds = [e[0] for e in h.events]
    assert kinds == ["recording_started", "recording_ended", "recording_lines"]
    assert h.events[1][1]["line_count"] == 2
    assert h.events[2][1]["lines"][0]["speaker"] == "1"
    assert h.finished == [record]
    assert await h.s.stop(END_VOICE) is None  # second stop is a no-op


@pytest.mark.asyncio
async def test_same_speaker_fragments_merge_into_one_line():
    h = make()
    await h.s.start()
    h.s.add_line("2", "And take your business.", 100, 1000)
    h.s.add_line("2", "To.", 1300, 1400)
    h.s.add_line("2", "The next level.", 1600, 2500)
    h.s.add_line("1", "Right.", 2600, 2900)  # speaker change: new line
    h.s.add_line("1", "Later.", 9000, 9500)  # gap > 1500ms: new line
    record = await h.s.stop(END_VOICE)
    assert [(l.speaker, l.text, l.end_ms) for l in record.lines] == [
        ("2", "And take your business. To. The next level.", 2500),
        ("1", "Right.", 2900),
        ("1", "Later.", 9500),
    ]


@pytest.mark.asyncio
async def test_lines_are_chunked_for_small_data_channels():
    h = make()
    await h.s.start()
    for i in range(LINES_PER_EVENT * 2 + 3):
        h.s.add_line(str(i % 2), f"l{i}", i * 5000, i * 5000 + 100)
    await h.s.stop(END_VOICE)
    chunks = [e for e in h.events if e[0] == "recording_lines"]
    assert [c[1]["chunk"] for c in chunks] == [1, 2, 3] and chunks[0][1]["chunks"] == 3
    assert len(chunks[2][1]["lines"]) == 3


@pytest.mark.asyncio
async def test_idle_predicate_composes_with_an_existing_one():
    h = make()
    pending = {"v": False}
    h.idle.suppress_when = lambda: pending["v"]
    await h.s.start()
    assert h.idle.suppress_when() is True
    await h.s.stop(END_VOICE)
    assert h.idle.suppress_when() is False
    pending["v"] = True
    assert h.idle.suppress_when() is True


@pytest.mark.asyncio
async def test_duration_cap_and_silence_end_the_recording():
    h = make(max_duration_seconds=1)
    await h.s.start()
    await asyncio.sleep(1.2)
    assert not h.s.is_recording and h.finished[0].end_reason == END_MAX_DURATION

    h2 = make(silence_timeout_seconds=1)
    await h2.s.start()
    await asyncio.sleep(1.3)
    assert not h2.s.is_recording and h2.finished[0].end_reason == END_SILENCE


# ── mute strategy + observer ─────────────────────────────────────────────


def push(frame) -> FramePushed:
    return FramePushed(
        source=cast(Any, None),
        destination=cast(Any, None),
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=0,
    )


def tok(text, speaker, start, end):
    return {"text": text, "speaker": speaker, "start_ms": start, "end_ms": end}


@pytest.mark.asyncio
async def test_mute_strategy_mutes_exactly_while_recording():
    h = make()
    strategy = MeetingRecordingMuteStrategy(h.s)
    frame = UserStartedSpeakingFrame()
    assert await strategy.process_frame(frame) is False
    await h.s.start()
    assert await strategy.process_frame(frame) is True
    await h.s.stop(END_VOICE)
    assert await strategy.process_frame(frame) is False


@pytest.mark.asyncio
async def test_observer_collects_by_speaker_dedupes_and_hears_stop():
    h = make()
    obs = MeetingRecordingObserver(h.s)

    before = TranscriptionFrame("hello there", "", "now", finalized=True)
    await obs.on_push_frame(push(before))  # not recording: ignored
    await h.s.start()

    tokens = [
        tok("Let's", "1", 60_000, 60_200),
        tok(" start.", "1", 60_200, 60_900),
        tok(" Sure,", "2", 61_000, 61_300),
        tok(" I'll share.", "2", 61_300, 62_000),
    ]
    frame = TranscriptionFrame(
        "Let's start. Sure, I'll share.", "", "now", result=tokens, finalized=True
    )
    await obs.on_push_frame(push(frame))
    await obs.on_push_frame(push(frame))  # same frame on the next link: ignored
    assert [(l.speaker, l.text, l.start_ms, l.end_ms) for l in h.s.lines] == [
        ("1", "Let's start.", 0, 900),
        ("2", "Sure, I'll share.", 1000, 2000),
    ]

    stop_frame = TranscriptionFrame(
        "Hey buddy, stop recording.", "", "now", finalized=True
    )
    await obs.on_push_frame(push(stop_frame))
    assert not h.s.is_recording
    assert h.finished[0].end_reason == END_VOICE and len(h.finished[0].lines) == 2

    # the stop sentence is a command: still muted for the LLM; later turns are not
    strategy = MeetingRecordingMuteStrategy(h.s)
    assert await strategy.process_frame(stop_frame) is True
    assert (
        await strategy.process_frame(TranscriptionFrame("what's next", "", "now"))
        is False
    )
