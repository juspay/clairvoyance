"""DragonTTS live stream: silence after the last sentence of each bot turn.

pipecat's telephony output drops the end of every turn (the 16k->8k stream
resampler's held-back samples + the <40 ms chunk remainder at bot-stopped),
and clean v3 clips end ~60 ms after the last word — so the last syllable was
cut on some calls. DragonTTSService queues DRAGONTTS_TURN_END_PAD_MS of
silence before TTSStoppedFrame. Pinned here: one pad per turn after its last
sentence (never between sentences), greetings included, read once per call,
at 0 exactly pipecat's own behavior, a config failure never breaks the call,
and the pad really saves the tail through pipecat's own resampler + chunking.
"""

from __future__ import annotations

import asyncio

import httpx
import numpy as np
import pytest
from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService

from app.ai.voice.tts import dragontts as dt
from app.core.config import dynamic

SR = 16000
SPEECH = b"\xe8\x03" * (SR // 2)  # 500 ms at a constant 1000, never silent


class _Recorder(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.log: list[str] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            ms = len(frame.audio) // 2 * 1000 // SR
            self.log.append(f"pad{ms}" if not any(frame.audio) else "speech")
        elif isinstance(frame, (TTSStartedFrame, TTSStoppedFrame)):
            self.log.append("start" if isinstance(frame, TTSStartedFrame) else "stop")
        await self.push_frame(frame, direction)


class _Errors(FrameProcessor):
    """Sits before the TTS service and keeps the errors it reports upstream."""

    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and direction == FrameDirection.UPSTREAM:
            self.errors.append(frame.error)
        await self.push_frame(frame, direction)


@pytest.fixture
def fake_dragontts(monkeypatch):
    """DragonTTS /tts/stream answers every sentence with 500 ms of speech."""
    real = httpx.AsyncClient
    sentences: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sentences.append(json.loads(request.content)["transcript"])
        return httpx.Response(200, content=SPEECH)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return sentences


def _pad(monkeypatch, value: int = 0, error: Exception | None = None) -> list[int]:
    reads: list[int] = []

    async def fake() -> int:
        reads.append(1)
        if error:
            raise error
        return value

    monkeypatch.setattr(dt, "DRAGONTTS_TURN_END_PAD_MS", fake)
    return reads


async def _run(frames_per_turn, errors: list[str] | None = None) -> list[str]:
    tts = dt.DragonTTSService(url="http://dragontts.test", model_id="m", voice_id="v")
    rec, err = _Recorder(), _Errors()
    task = PipelineTask(
        Pipeline([err, tts, rec]), params=PipelineParams(), check_dangling_tasks=False
    )

    async def feed():
        for frames in frames_per_turn:
            for frame in frames:
                await task.queue_frame(frame)
            await asyncio.sleep(0.3)
        await task.queue_frame(EndFrame())

    await asyncio.gather(PipelineRunner(handle_sigint=False).run(task), feed())
    if errors is not None:
        errors.extend(err.errors)
    return rec.log


def _llm(*tokens: str) -> list:
    return [
        LLMFullResponseStartFrame(),
        *(TextFrame(t) for t in tokens),
        LLMFullResponseEndFrame(),
    ]


async def test_one_pad_per_turn_after_its_last_sentence(monkeypatch, fake_dragontts):
    reads = _pad(monkeypatch, 100)
    log = await _run([_llm("Hi", ". ", "Hello", "."), _llm("Tell", " me", ".")])
    assert fake_dragontts == ["Hi.", "Hello.", "Tell me."]
    assert log == [
        "start", "speech", "speech", "pad100", "stop",  # nothing between Hi / Hello
        "start", "speech", "pad100", "stop",
    ]  # fmt: skip
    assert len(reads) == 1  # once per call, not per turn


async def test_a_spoken_line_like_the_greeting_is_padded_too(
    monkeypatch, fake_dragontts
):
    _pad(monkeypatch, 150)
    log = await _run([[TTSSpeakFrame("नमस्ते, मैं Flipkart से बोल रही हूँ.")]])
    assert log == ["start", "speech", "pad150", "stop"]


TURNS = [
    _llm("Hi", ". ", "Hello", "."),
    [TTSSpeakFrame("नमस्ते.")],
    _llm("Tell", " me", " more", "."),
]


async def test_zero_is_exactly_pipecats_own_behavior(monkeypatch, fake_dragontts):
    _pad(monkeypatch, 0)
    padded_off = await _run(TURNS)
    # The same call with the override removed = the code before this change.
    monkeypatch.setattr(
        dt.DragonTTSService,
        "on_turn_context_completed",
        TTSService.on_turn_context_completed,
    )
    before = await _run(TURNS)
    assert padded_off == before
    assert "pad" not in " ".join(padded_off)


async def test_a_config_failure_never_breaks_the_call(monkeypatch, fake_dragontts):
    _pad(monkeypatch, error=RuntimeError("redis down"))
    errors: list[str] = []
    log = await _run([_llm("Hi", ".")], errors)
    assert log == ["start", "speech", "stop"]  # spoken, unpadded
    assert errors == []  # nothing reported up the pipeline


async def test_start_survives_a_config_failure_with_the_pad_off(monkeypatch):
    _pad(monkeypatch, error=RuntimeError("redis down"))

    async def base_start(self, frame):  # pipecat's part needs a running pipeline
        pass

    monkeypatch.setattr(TTSService, "start", base_start)
    tts = dt.DragonTTSService(url="http://dragontts.test", model_id="m", voice_id="v")
    try:
        await tts.start(StartFrame(audio_out_sample_rate=8000))  # must not raise
        assert tts._turn_end_pad_ms == 0
    finally:
        await tts._close_client()


@pytest.mark.parametrize(
    "raw, expected", [(None, 160), (250, 250), (0, 0), (900, 500), (-5, 0), ("x", 160)]
)
async def test_the_dynamic_config_defaults_to_160_and_is_clamped(
    monkeypatch, raw, expected
):
    async def fake_get_config(key, default, return_type=str):
        assert key == "DRAGONTTS_TURN_END_PAD_MS" and default == 160
        return default if raw is None else raw

    monkeypatch.setattr(dynamic, "get_config", fake_get_config)
    assert await dynamic.DRAGONTTS_TURN_END_PAD_MS() == expected


def _played(clip: np.ndarray, pad_ms: int, earlier_ms: int) -> np.ndarray:
    """What pipecat's telephony output sends of `clip` at the end of a turn:
    its stream resampler 16k->8k, 40 ms chunks, remainder dropped at
    bot-stopped (transports/base_output.py)."""

    async def run() -> bytes:
        rs, buf, sent = SOXRStreamAudioResampler(), bytearray(), bytearray()
        earlier = np.full(SR * earlier_ms // 1000, 1000, "<i2")
        pad = np.zeros(SR * pad_ms // 1000, "<i2")
        for part in (earlier, clip, pad):
            for i in range(0, len(part), 8192):  # DragonTTS stream chunks
                buf.extend(await rs.resample(part[i : i + 8192].tobytes(), SR, 8000))
                while len(buf) >= 640:  # 40 ms at 8 kHz
                    sent.extend(buf[:640])
                    buf = buf[640:]
        return bytes(sent)  # `buf` is what bot-stopped throws away

    out = np.frombuffer(asyncio.run(run()), "<i2")
    return out[8000 * earlier_ms // 1000 :]


def test_the_pad_saves_the_last_word_through_pipecats_output():
    """A clean v3 clip: voice up to 60 ms before its end. Over turns whose
    earlier sentences differ in length, pipecat's output cuts the voice on
    some turns unpadded (the intermittent cut) and on none with the 160 ms
    pad."""
    t = np.arange(SR) / SR
    voice = (8000 * np.sin(2 * np.pi * 220 * t)).astype("<i2")  # 1 s of "voice"
    clip = np.concatenate([voice, np.zeros(SR * 60 // 1000, "<i2")])

    def voice_played(pad_ms: int, earlier_ms: int) -> int:
        out = _played(clip, pad_ms=pad_ms, earlier_ms=earlier_ms)[:8000]
        return int(np.count_nonzero(np.abs(out) > 200))

    cut_turns = 0
    for earlier_ms in range(0, 3000, 110):
        full = voice_played(500, earlier_ms)
        assert voice_played(160, earlier_ms) == full, earlier_ms
        cut_turns += voice_played(0, earlier_ms) < full
    assert cut_turns > 0  # the cut this fixes, on some turns only
