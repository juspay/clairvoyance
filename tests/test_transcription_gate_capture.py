"""Capture mode on TranscriptionGateProcessor: lines out, nothing downstream,
the stop phrase as a whole sentence, and inert when not engaged."""

import pytest
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.processors.transcription_gate import (
    CaptureLine,
    TranscriptionGateProcessor,
)

STOP = "hey buddy stop recording the meeting"


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.pushed: list = []
        self.lines: list[CaptureLine] = []
        self.interims: list[str] = []
        self.stops = 0

        async def capture_push(proc, frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        monkeypatch.setattr(FrameProcessor, "push_frame", capture_push)
        self.gate = TranscriptionGateProcessor()

    async def on_line(self, line: CaptureLine) -> None:
        self.lines.append(line)

    async def on_stop(self) -> None:
        self.stops += 1
        self.gate.stop_capture()

    async def on_interim(self, text: str) -> None:
        self.interims.append(text)

    def start(self, threshold: float = 0.9) -> None:
        self.gate.start_capture(
            on_line=self.on_line,
            on_stop=self.on_stop,
            stop_phrase=STOP,
            threshold=threshold,
            on_interim=self.on_interim,
        )

    async def final(self, text: str, tokens=None) -> None:
        await self.gate.process_frame(
            TranscriptionFrame(text, "", "now", result=tokens, finalized=True),
            FrameDirection.DOWNSTREAM,
        )

    async def interim(self, text: str) -> None:
        await self.gate.process_frame(
            InterimTranscriptionFrame(text, "", "now"), FrameDirection.DOWNSTREAM
        )


def tok(text: str, speaker: str | None, start: int, end: int) -> dict:
    return {"text": text, "speaker": speaker, "start_ms": start, "end_ms": end}


@pytest.mark.asyncio
async def test_not_capturing_is_a_pass_through(monkeypatch):
    h = Harness(monkeypatch)
    await h.final("hello buddy")
    await h.interim("hel")
    assert [f.text for f in h.pushed] == ["hello buddy", "hel"]
    assert h.lines == [] and h.stops == 0 and not h.gate.is_capturing


@pytest.mark.asyncio
async def test_capturing_diverts_finals_to_lines_and_pushes_nothing(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    await h.final("Let's start with last week's numbers.")
    await h.interim("sure i")
    assert h.pushed == []
    assert [l.text for l in h.lines] == ["Let's start with last week's numbers."]
    assert h.lines[0].speaker is None
    assert h.interims == ["sure i"]


@pytest.mark.asyncio
async def test_one_frame_with_two_speakers_becomes_two_lines(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    tokens = [
        tok("Let's", "1", 60_000, 60_200),
        tok(" start.", "1", 60_200, 60_900),
        tok(" Sure,", "2", 61_000, 61_300),
        tok(" I'll share.", "2", 61_300, 62_000),
    ]
    await h.final("Let's start. Sure, I'll share.", tokens)
    assert [(l.speaker, l.text) for l in h.lines] == [
        ("1", "Let's start."),
        ("2", "Sure, I'll share."),
    ]
    # offsets are from the first token after capture started, not the STT clock
    assert (h.lines[0].start_ms, h.lines[0].end_ms) == (0, 900)
    assert (h.lines[1].start_ms, h.lines[1].end_ms) == (1000, 2000)


@pytest.mark.asyncio
async def test_stop_phrase_matches_as_a_whole_sentence_with_slips(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    await h.final("Hey Buddy, stop recording the meeting.")
    assert h.stops == 1 and h.lines == [] and not h.gate.is_capturing

    h.start()
    await h.final("hey body stop recording the meeting")  # STT slip
    assert h.stops == 2 and h.lines == []


@pytest.mark.asyncio
async def test_a_sentence_that_merely_contains_the_phrase_is_a_line(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    await h.final(
        "I think we should tell hey buddy stop recording the meeting only at the very end"
    )
    assert h.stops == 0 and len(h.lines) == 1 and h.gate.is_capturing


@pytest.mark.asyncio
async def test_after_stop_the_gate_passes_through_again(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    await h.final("hey buddy stop recording the meeting")
    await h.final("what's next?")
    assert [f.text for f in h.pushed] == ["what's next?"]


@pytest.mark.asyncio
async def test_hard_mute_wins_over_capture(monkeypatch):
    h = Harness(monkeypatch)
    h.start()
    h.gate.mute()
    await h.final("this is said while muted")
    assert h.lines == [] and h.pushed == []


def test_start_capture_validates_its_inputs():
    gate = TranscriptionGateProcessor()

    async def noop(*_):
        return None

    with pytest.raises(ValueError):
        gate.start_capture(on_line=noop, on_stop=noop, stop_phrase="   ")
    with pytest.raises(ValueError):
        gate.start_capture(on_line=noop, on_stop=noop, stop_phrase="x", threshold=0)
    gate.stop_capture()  # safe when not capturing
    assert not gate.is_capturing and gate.capture_elapsed_ms() == 0


@pytest.mark.asyncio
async def test_any_phrase_in_the_list_stops_including_short_ones(monkeypatch):
    h = Harness(monkeypatch)
    h.gate.start_capture(
        on_line=h.on_line,
        on_stop=h.on_stop,
        stop_phrase=[STOP, "hey buddy stop recording", "stop recording"],
    )
    await h.final("Hey buddy, stop recording.")
    assert h.stops == 1 and h.lines == []
    h.gate.start_capture(
        on_line=h.on_line,
        on_stop=h.on_stop,
        stop_phrase=[STOP, "hey buddy stop recording", "stop recording"],
    )
    await h.final("we should stop recording these numbers at some point")
    assert h.stops == 1 and len(h.lines) == 1  # a longer sentence is a line
