"""AIC noise filter must fail open: an SDK error never ends or deafens a call.

The ai-coustics license can be revoked at any time. pipecat's AICFilter lets a
corrupt model raise out of start() (transport never starts) and lets a mid-call
SDK error raise out of filter() (input audio loop dies, bot goes deaf). The
wrapper turns both into "no noise cancellation for the rest of this call".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aic_sdk
import numpy as np
import pytest
from pipecat.audio.filters.aic_filter import AICModelManager
from pipecat.frames.frames import FilterEnableFrame

from app.ai.voice.agents.breeze_buddy.utils.aic_filter import FailOpenAICFilter

SAMPLE_RATE = 8000
BLOCK = 80  # frames per AIC block (10 ms at 8 kHz)
FRAME = (np.arange(160, dtype=np.int16) * 7).tobytes()  # 20 ms, 2 blocks


class FakeProcessor:
    """Stands in for aic_sdk.ProcessorAsync: identity output, optional failure."""

    def __init__(
        self, fail_after: int | None = None, error: BaseException | None = None
    ):
        self.calls = 0
        self.fail_after = fail_after
        self.error = error or aic_sdk.EnhancementNotAllowedError(
            "SDK key was not authorized or process failed to report usage"
        )

    async def process_async(self, buf):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise self.error
        self.calls += 1
        return buf.copy()


class FakeContext:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def set_parameter(self, *_):
        if self.error:
            raise self.error

    def reset(self):
        if self.error:
            raise self.error


def ready_filter(processor: FakeProcessor, ctx: FakeContext | None = None):
    """A filter in the state AICFilter.start() leaves it in, minus the SDK."""
    f = FailOpenAICFilter(license_key="test", model_path=Path("unused.aicmodel"))
    f._sample_rate = SAMPLE_RATE
    f._processor = processor
    f._processor_ctx = ctx
    f._aic_ready = True
    f._frames_per_block = BLOCK
    f._in_f32 = np.zeros((1, BLOCK), dtype=np.float32)
    f._out_i16 = np.zeros(BLOCK, dtype=np.int16)
    return f


async def test_healthy_filter_output_is_unchanged():
    f = ready_filter(FakeProcessor())
    assert await f.filter(FRAME) == FRAME
    assert not f._failed


async def test_corrupt_model_at_start_falls_back_to_passthrough(tmp_path: Path):
    """Real SDK: a garbage model file raises from start() in plain AICFilter."""
    bad = tmp_path / "corrupt.aicmodel"
    bad.write_bytes(b"garbage" * 100)
    f = FailOpenAICFilter(license_key="test", model_path=bad)

    await f.start(SAMPLE_RATE)  # must not raise

    assert f._failed
    assert await f.filter(FRAME) == FRAME
    assert not any(str(bad.name) in key for key in AICModelManager._cache)
    await f.stop()  # the transport still calls stop(); must stay harmless


async def test_mid_call_revocation_returns_raw_audio_then_passes_through():
    proc = FakeProcessor(fail_after=1)
    f = ready_filter(proc)
    half_block = FRAME[:BLOCK]  # 40 frames: buffered, not yet processed
    assert await f.filter(half_block) == b""

    # Next frame completes blocks; the second block raises mid-loop. Everything
    # the filter was holding comes back raw, in order, nothing dropped.
    out = await f.filter(FRAME)
    assert out == half_block + FRAME
    assert f._failed
    assert len(f._audio_buffer) == 0

    calls = proc.calls
    assert await f.filter(FRAME) == FRAME
    assert proc.calls == calls  # SDK never touched again this call


async def test_failure_is_logged_once(monkeypatch):
    errors = []
    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.utils.aic_filter.logger.error",
        lambda *a, **k: errors.append(a),
    )
    f = ready_filter(FakeProcessor(fail_after=0))
    for _ in range(5):
        await f.filter(FRAME)
    assert len(errors) == 1


async def test_cancellation_is_not_swallowed():
    f = ready_filter(FakeProcessor(fail_after=0, error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await f.filter(FRAME)


async def test_control_frame_error_keeps_audio_flowing():
    f = ready_filter(FakeProcessor(), FakeContext(RuntimeError("revoked")))
    await f.process_frame(FilterEnableFrame(enable=False))
    # pipecat already swallows set_parameter errors here; either way the
    # control path must not raise and audio must keep flowing.
    assert await f.filter(FRAME)


async def test_stop_never_raises():
    f = ready_filter(FakeProcessor(), FakeContext(RuntimeError("revoked")))
    await f.stop()
    await f.stop()
    assert f._processor is None
