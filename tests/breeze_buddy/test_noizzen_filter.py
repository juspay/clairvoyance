"""The Noizzen noise filter keeps pipecat's audio contract and never deafens a call.

Pins: the framing is exact (an identity model comes back at its look-ahead, a
partial blend without echo); near-silence skips the engine without moving the
output, and speech after it comes back exact; every failure and overload degrades
to passthrough without losing or repeating a sample; disabling passes the caller
through at the same delay; a split sample never shifts the stream; the template
alone picks the filter (noizzen gets Noizzen; aic and legacy shapes get ai-coustics,
or Noizzen when it cannot run), AIC failures hand the call to Noizzen, AIC teardown
leaves the event loop, and the AIC wrapper holds on every pipecat from 1.1.0 to 1.15.0.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.frames.frames import FilterEnableFrame
from pydantic import ValidationError

# isort: off
# template.types must load before the agent modules (circular import otherwise).
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    NoiseFilterConfig,
    NoiseFilterModel,
    NoiseFilterProvider,
)

from app.ai.voice.agents.breeze_buddy.agent import transport
from app.ai.voice.agents.breeze_buddy.agent.transport import (
    TRANSPORT_TYPE_DAILY,
    TRANSPORT_TYPE_TELEPHONY,
    _create_audio_input_filter,
    _FailOpenAICFilter,
)

# isort: on
from app.ai.voice.noise_filter.noizzen import (
    MODELS,
    NoizzenFilter,
    filter as filter_module,
    models,
)
from app.ai.voice.noise_filter.noizzen.filter import StreamEnhancer
from app.ai.voice.noise_filter.noizzen.models import (
    LoadedModel,
    ModelSpec,
    vorbis_window,
)

CHUNK = 160  # 20 ms at 8 kHz, what the telephony serializers hand pipecat


def _speechlike(seconds: float, rate: int = 8000) -> np.ndarray:
    """Smoothly gated in-band partials (300-3200 Hz), like voiced speech on a phone line."""
    rng = np.random.default_rng(0)
    t = np.arange(int(seconds * rate)) / rate
    freqs = rng.uniform(300, 3200, 12)
    phases = rng.uniform(0, 2 * np.pi, 12)
    x = np.sum(np.sin(2 * np.pi * freqs[:, None] * t + phases[:, None]), axis=0)
    syllables = 0.5 - 0.5 * np.cos(2 * np.pi * 3 * t)  # smooth, so no clicks
    return (0.03 * x * syllables).astype(np.float32)


def _pcm(x: np.ndarray) -> bytes:
    return (x * 32767).astype(np.int16).tobytes()


def _stream(enhancer: StreamEnhancer, x: np.ndarray, step: int) -> np.ndarray:
    parts = [enhancer.process(x[i : i + step]) for i in range(0, x.size, step)]
    return np.concatenate(parts)


def _assert_input_back(y: np.ndarray, x: np.ndarray, delay: int, warm: int = 1024):
    n = min(y.size - delay, x.size)
    err = np.max(np.abs(y[delay + warm : delay + n] - x[warm:n]))
    assert err < 0.01 * np.max(np.abs(x))


class _LookAheadStream:
    """Stands in for an engine stream that returns its input spectrum `hops` frames late."""

    def __init__(self, bins: int, hops: int, poison: bool = False):
        self._past = deque([np.zeros((bins, 2), np.float32)] * hops)
        self._poison = poison
        self.steps = 0

    def step(self, ri):
        self.steps += 1
        self._past.append(ri.copy())
        out = self._past.popleft()
        return np.full_like(out, np.nan) if self._poison else out


def _fake_model(rate: int = 8000, hops: int = 4, poison: bool = False) -> LoadedModel:
    """An identity 'model' with DPDFNet's framing (20 ms window, 10 ms hop)."""
    win, hop = rate // 50, rate // 100
    bins = win // 2 + 1
    spec = ModelSpec("fake", "fake", rate, win, hop, hops)
    model = SimpleNamespace(
        spec=spec,
        window=vorbis_window(win),
        open_stream=lambda: _LookAheadStream(bins, hops, poison),
    )
    return cast(LoadedModel, cast(Any, model))


@pytest.fixture
def fake_load(monkeypatch):
    """NoizzenFilter loads the fake model instead of an ONNX file."""
    monkeypatch.setattr(filter_module, "load_model", lambda name, path: _fake_model())


async def _feed(f, pcm: bytes) -> list:
    step = CHUNK * 2
    return [await f.filter(pcm[i : i + step]) for i in range(0, len(pcm), step)]


def _filter(**kwargs) -> NoizzenFilter:
    return NoizzenFilter(model="dpdfnet2_8k", model_path=Path("unused"), **kwargs)


@pytest.mark.parametrize("mix", [1.0, 0.75, 0.5])
@pytest.mark.parametrize("rate", [8000, 16000])
def test_engine_is_exact_at_the_look_ahead(mix, rate):
    """Identity model 4 hops late: the input comes back 4 hops later, and a
    partial blend lines the noisy frame up with it instead of echoing 40 ms apart."""
    x = _speechlike(2.0, rate)
    model = _fake_model(rate, 4)
    y = _stream(StreamEnhancer(model, rate, mix=mix), x, rate // 50)
    _assert_input_back(y, x, model.spec.delay_hops * model.spec.hop)


def _tone(seconds: float, dbfs: float) -> np.ndarray:
    """A steady 1 kHz tone at 8 kHz whose mean power is `dbfs` in every hop."""
    t = np.arange(int(seconds * 8000)) / 8000
    return (np.sqrt(2 * 10 ** (dbfs / 10)) * np.sin(2 * np.pi * 1000 * t)).astype(
        np.float32
    )


def _steps(enhancer: StreamEnhancer) -> int:
    return cast(Any, enhancer._engine).steps


@pytest.mark.parametrize("mix", [1.0, 0.75, 0.5])
def test_near_silence_skips_the_engine_and_keeps_the_delay(mix):
    """Below the gate the engine never runs and the output is (1 - mix) x the
    input at the same delay: what the model's near-zero output blends to."""
    x = _tone(1.0, filter_module.GATE_DBFS - 3)
    model = _fake_model()
    enhancer = StreamEnhancer(model, 8000, mix=mix)
    y = _stream(enhancer, x, CHUNK)
    assert _steps(enhancer) == 0
    delay = model.spec.delay_hops * model.spec.hop
    want = (1.0 - mix) * x[: y.size - delay]
    np.testing.assert_allclose(y[delay + 1024 :], want[1024:], atol=1e-6)


@pytest.mark.parametrize("offset_db, runs", [(1.0, True), (-1.0, False)])
def test_the_engine_runs_on_hops_above_the_gate(offset_db, runs):
    enhancer = StreamEnhancer(_fake_model(), 8000, mix=0.5)
    y = _stream(enhancer, _tone(0.5, filter_module.GATE_DBFS + offset_db), CHUNK)
    hops = y.size // enhancer._hop
    assert _steps(enhancer) == (hops if runs else 0)


@pytest.mark.parametrize("mix", [1.0, 0.5])
def test_speech_after_silence_comes_back_exact_and_on_time(mix):
    """The gate reads the newest hop, delay_hops ahead of the output, so the
    engine is already running when the first loud hop is output."""
    x = np.concatenate([np.zeros(8000, np.float32), _tone(0.5, -30.0)])
    model = _fake_model()
    y = _stream(StreamEnhancer(model, 8000, mix=mix), x, CHUNK)
    delay = model.spec.delay_hops * model.spec.hop
    np.testing.assert_allclose(y[delay:], x[: y.size - delay], atol=1e-6)


def test_the_engine_runs_through_pauses_and_stops_after_the_hold():
    loud = _tone(0.5, -30.0)
    pause = np.zeros(1200, np.float32)  # 150 ms, inside the 200 ms hold
    enhancer = StreamEnhancer(_fake_model(), 8000, mix=0.5)
    _stream(enhancer, np.concatenate([loud, pause, loud]), CHUNK)
    talking = _steps(enhancer)
    frames = (2 * loud.size + pause.size - enhancer._win) // enhancer._hop + 1
    assert talking == frames  # no hop skipped in the pause
    _stream(enhancer, np.zeros(8000, np.float32), CHUNK)
    # the frame holding the last loud hop is the first of the hold
    assert _steps(enhancer) == talking + filter_module.GATE_HOLD_HOPS - 1


async def test_a_call_at_another_rate_than_the_model_passes_through(monkeypatch):
    monkeypatch.setattr(
        filter_module, "load_model", lambda name, path: _fake_model(16000)
    )
    f = _filter()
    await f.start(8000)
    chunk = _pcm(_speechlike(0.02))
    assert f._enhancer is None and await f.filter(chunk) == chunk


def test_dpdfnet_specs_report_their_look_ahead():
    assert MODELS["dpdfnet2_8k"].delay_hops == 4
    assert MODELS["dpdfnet_base_16k"].delay_hops == 4


async def test_filter_speaks_int16_bytes_with_bounded_latency(fake_load):
    f = _filter()
    await f.start(8000)
    pcm = _pcm(_speechlike(1.0))
    out = await _feed(f, pcm)
    assert all(len(o) % 2 == 0 for o in out)
    held_back = len(pcm) - sum(len(o) for o in out)
    assert 0 <= held_back <= 40 * 8 * 2  # never more than one window + look-ahead
    await f.stop()


async def test_filter_fails_open_for_the_rest_of_the_call(fake_load, monkeypatch):
    f = _filter()
    await f.start(8000)

    def boom(_enhancer, _x):
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(f, "_process", boom)
    chunk = _pcm(_speechlike(0.02))
    assert await f.filter(chunk) == chunk
    assert f._enhancer is None  # stays passthrough, no retry storm


async def test_non_finite_model_output_fails_open(fake_load):
    f = _filter()
    await f.start(8000)
    f._enhancer = StreamEnhancer(_fake_model(hops=0, poison=True), 8000)
    pcm = _pcm(_speechlike(0.1))
    outs = await _feed(f, pcm)
    assert f._enhancer is None
    assert outs[-1] == pcm[-CHUNK * 2 :]  # the caller is heard, not silenced


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, np.int16).astype(np.int32)


async def test_a_frame_that_splits_a_sample_keeps_the_stream_in_line(fake_load):
    """The odd byte waits for the next frame: the output is exactly what
    whole-sample frames give, and filtering stays on."""
    pcm = _pcm(_speechlike(0.5))
    whole = _filter()
    await whole.start(8000)
    want = b"".join(await _feed(whole, pcm))
    split = _filter()
    await split.start(8000)
    got, i = [], 0
    for size in itertools.cycle([321, 1, 319, 163, 157, 3]):
        if i >= len(pcm):
            break
        got.append(await split.filter(pcm[i : i + size]))
        i += size
    assert b"".join(got) == want
    assert split._enhancer is not None


async def test_disabling_passes_the_caller_through_at_the_same_delay(fake_load):
    """FilterEnableFrame(enable=False) keeps the framing and delay and skips the
    model: the original comes out whole, so enabling again emits nothing stale."""
    f = _filter()
    await f.start(8000)
    await f.process_frame(FilterEnableFrame(enable=False))
    pcm = _pcm(_speechlike(1.0))
    y, x, d = _samples(b"".join(await _feed(f, pcm))), _samples(pcm), f._delay
    # past the first window (no half before it to overlap); within int16 rounding
    assert np.abs(y[d + CHUNK :] - x[CHUNK : y.size - d]).max() <= 1
    stats = f._stats
    assert stats is not None and stats.skipped == stats.hops  # the model never ran


async def test_enabling_again_resumes_the_model_on_the_same_stream(fake_load):
    """Disabled, then enabled: the framing never stopped, so nothing is dropped,
    repeated or stale; the model just runs again."""
    f = _filter()
    await f.start(8000)
    pcm = _pcm(_speechlike(1.5))
    third = len(pcm) // 3
    out = await _feed(f, pcm[:third])
    await f.process_frame(FilterEnableFrame(enable=False))
    out += await _feed(f, pcm[third : 2 * third])
    enhancer = f._enhancer
    assert enhancer is not None
    paused = _steps(enhancer)
    await f.process_frame(FilterEnableFrame(enable=True))
    out += await _feed(f, pcm[2 * third :])
    assert _steps(enhancer) > paused
    assert sum(len(o) for o in out) == len(pcm) - 160  # only the 10 ms framing hold


@pytest.mark.parametrize("cause", ["error", "lag"])
@pytest.mark.parametrize("disabled", [True, False])
async def test_switching_off_hands_back_the_audio_the_filter_held(
    fake_load, monkeypatch, cause, disabled
):
    """The ~50 ms inside the filter (look-ahead and framing) goes out at the
    switch-off, then the caller passes through: no sample lost or repeated, only
    the delay goes. Disabled, the output is the input and checks sample by sample."""
    f = _filter()
    await f.start(8000)
    if disabled:
        await f.process_frame(FilterEnableFrame(enable=False))

    def boom(enhancer, x):
        raise RuntimeError("engine exploded")

    pcm, step, out = _pcm(_speechlike(1.0)), CHUNK * 2, []
    for n, i in enumerate(range(0, len(pcm), step)):
        if n == 20:  # mid-call, well past the start-up delay
            if cause == "error":
                monkeypatch.setattr(f, "_process", boom)
            else:
                monkeypatch.setattr(filter_module, "LAG_BUDGET_S", -1.0)
        out.append(await f.filter(pcm[i : i + step]))
    assert f._enhancer is None and f._off is not None
    y, x, d = _samples(b"".join(out)), _samples(pcm), f._delay
    assert y.size == x.size + d  # every input sample exactly once, after the delay
    if disabled:  # past the first window, within int16 rounding
        assert np.abs(y[d + CHUNK :] - x[CHUNK:]).max() <= 1


async def test_the_held_audio_stays_small_on_a_long_call(fake_load):
    f = _filter()
    await f.start(8000)
    await _feed(f, _pcm(_speechlike(10.0)))
    enhancer = f._enhancer
    assert enhancer is not None
    # the look-ahead plus less than one window of framing, however long the call
    assert len(f._raw) < 2 * (f._delay + enhancer._win)


def test_the_pool_has_a_worker_per_cpu():
    """Calls sharing a process (an API pod when no voice pod is free) must not
    queue behind a fixed pool while CPUs sit idle."""
    workers = filter_module._EXECUTOR._max_workers
    assert workers == min(32, max(4, os.cpu_count() or 4))


async def test_falling_behind_real_time_switches_to_passthrough(fake_load, monkeypatch):
    f = _filter()
    await f.start(8000)
    original = f._process

    def slow(enhancer, x):
        time.sleep(0.035)  # 35 ms of work per 20 ms of audio: the pod can't keep up
        return original(enhancer, x)

    monkeypatch.setattr(f, "_process", slow)
    chunk = _pcm(_speechlike(0.02))
    for _ in range(12):
        await f.filter(chunk)
    assert f._enhancer is None
    assert f._lag > filter_module.LAG_BUDGET_S


class _Log:
    """Stands in for the module's logger and keeps every message."""

    def __init__(self):
        self.lines: list = []

    def info(self, msg, *args, **kwargs):
        self.lines.append(msg)

    warning = error = info


async def test_each_call_ends_with_one_summary_line(fake_load, monkeypatch):
    """The skip share per call is what the canary compares per template."""
    log = _Log()
    monkeypatch.setattr(filter_module, "logger", log)
    f = _filter()
    await f.start(8000)
    speech_then_silence = np.concatenate([_speechlike(0.5), np.zeros(8000, np.float32)])
    await _feed(f, _pcm(speech_then_silence))
    stats = f._stats
    assert stats is not None and stats.hops == 149 and 0 < stats.skipped < stats.hops
    await f.stop()
    assert log.lines == [
        "Noizzen summary: engine skipped on "
        f"{100 * stats.skipped / stats.hops:.0f}% of 149 steps; filter on to the end"
    ]


async def test_the_summary_says_why_filtering_switched_off(fake_load, monkeypatch):
    log = _Log()
    monkeypatch.setattr(filter_module, "logger", log)
    f = _filter()
    await f.start(8000)
    original = f._process

    def slow(enhancer, x):
        time.sleep(0.035)
        return original(enhancer, x)

    monkeypatch.setattr(f, "_process", slow)
    for _ in range(12):
        await f.filter(_pcm(_speechlike(0.02)))
    await f.stop()
    assert log.lines[-1].startswith("Noizzen summary: ")
    assert "filter switched off: fell" in log.lines[-1]


async def test_an_event_loop_stall_does_not_switch_filtering_off(
    fake_load, monkeypatch
):
    """A stall elsewhere on the loop is not the filter falling behind."""
    import asyncio

    f = _filter()
    await f.start(8000)
    original = f._process

    def slow(enhancer, x):
        time.sleep(0.01)  # keeps the await open while the loop stalls
        return original(enhancer, x)

    monkeypatch.setattr(f, "_process", slow)
    chunk = _pcm(_speechlike(0.02))
    task = asyncio.ensure_future(f.filter(chunk))
    await asyncio.sleep(0)  # the frame is now on the worker
    time.sleep(0.15)  # another coroutine blocks the loop for 150 ms
    await task
    assert f._enhancer is not None
    assert f._lag < filter_module.LAG_BUDGET_S


async def test_a_model_that_will_not_load_means_passthrough():
    f = NoizzenFilter(model="dpdfnet2_8k", model_path=Path("/nonexistent.onnx"))
    await f.start(8000)
    chunk = _pcm(_speechlike(0.02))
    assert await f.filter(chunk) == chunk


async def test_a_failed_load_does_not_keep_every_call_alive(tmp_path):
    """The cached load error is re-raised per call; its traceback must not grow."""
    import gc
    import weakref

    bad = tmp_path / "bad.weights"
    bad.write_bytes(b"not a model")
    refs = []
    for _ in range(5):
        f = NoizzenFilter(model="dpdfnet2_8k", model_path=bad)
        await f.start(8000)
        refs.append(weakref.ref(f))
        del f
    gc.collect()
    assert sum(r() is not None for r in refs) <= 1  # only the latest call's frames
    models._failed.pop(("dpdfnet2_8k", str(bad)), None)


def test_a_failed_load_is_retried_after_a_minute(monkeypatch):
    """A broken file is not re-read on every call, and a one-off error (memory,
    file handles) does not leave the process unfiltered until it restarts."""
    tries = []

    def flaky(name, path):
        tries.append(path)
        if len(tries) < 3:
            raise OSError(24, "Too many open files")
        return "loaded"

    now = [1000.0]
    monkeypatch.setattr(models, "_load", flaky)
    monkeypatch.setattr(models.time, "monotonic", lambda: now[0])
    key = ("dpdfnet2_8k", "flaky.weights")
    try:
        with pytest.raises(OSError) as first:
            models.load_model(*key)
        with pytest.raises(OSError) as second:
            models.load_model(*key)
        assert len(tries) == 1  # inside the minute: not re-read
        assert second.value is not first.value  # each call gets its own error
        assert second.value.errno == 24
        now[0] += models.FAILED_LOAD_RETRY_S
        with pytest.raises(OSError):
            models.load_model(*key)
        assert len(tries) == 2
        now[0] += models.FAILED_LOAD_RETRY_S
        assert cast(Any, models.load_model(*key)) == "loaded"
        assert key not in models._failed
    finally:
        models._failed.pop(key, None)


async def test_default_level_blends_and_zero_skips_the_model(fake_load):
    assert _filter()._mix == filter_module.DEFAULT_LEVEL == 0.5
    f = _filter(enhancement_level=0.0)
    await f.start(8000)
    assert f._enhancer is None


@pytest.fixture
def provisioned(monkeypatch, tmp_path):
    """Both DPDFNet engines and weights present where the Docker build puts them."""
    for spec in MODELS.values():
        (tmp_path / spec.file).write_bytes(b"weights")
        for variant in models.cpu_variants():
            (tmp_path / f"{spec.stem}.{variant}.so").write_bytes(b"engine")
    monkeypatch.setattr(transport.static, "BREEZE_BUDDY_NOISE_MODEL_DIR", str(tmp_path))
    return tmp_path


def _configs(**noise_filter) -> ConfigurationModel:
    cfg = NoiseFilterConfig(enable=True, **noise_filter)
    return cast(ConfigurationModel, SimpleNamespace(noise_filter=cfg))


STORED_SHAPES = [
    {"provider": "noizzen", "model": "noise_cancellation"},
    {"provider": "aic", "model": "noise_cancellation"},
    {"provider": "aic", "model": "noise_cancellation_s"},
    {"provider": "aic", "model": "voice_focus"},
    {"type": "aic"},
    {"type": "aic_voice_focus"},
]


TRANSPORT_MODELS = [
    (TRANSPORT_TYPE_TELEPHONY, "dpdfnet2_8k"),
    (TRANSPORT_TYPE_DAILY, "dpdfnet_base_16k"),
]


@pytest.mark.parametrize("transport_type, model", TRANSPORT_MODELS)
def test_noizzen_templates_get_noizzen(provisioned, transport_type, model):
    f = _create_audio_input_filter(_configs(**STORED_SHAPES[0]), transport_type)
    assert isinstance(f, NoizzenFilter)
    assert f._model_name == model


@pytest.mark.parametrize("shape", STORED_SHAPES[1:])  # every "aic" shape
@pytest.mark.parametrize("transport_type, model", TRANSPORT_MODELS)
def test_aic_templates_without_a_license_still_get_a_filter(
    provisioned, monkeypatch, shape, transport_type, model
):
    """No ai-coustics license (AWS builds, or a removed key): Noizzen, not silence."""
    monkeypatch.setattr(transport.static, "BREEZE_BUDDY_AIC_LICENSE_KEY", "")
    f = _create_audio_input_filter(_configs(**shape), transport_type)
    assert isinstance(f, NoizzenFilter)
    assert f._model_name == model


def test_ai_coustics_enhancement_level_is_not_reused(provisioned, monkeypatch):
    """AIC's level is on another scale: an aic template's Noizzen fallback ignores it."""
    monkeypatch.setattr(transport.static, "BREEZE_BUDDY_AIC_LICENSE_KEY", "")
    aic = _create_audio_input_filter(
        _configs(provider="aic", model="noise_cancellation", enhancement_level=0.3),
        TRANSPORT_TYPE_TELEPHONY,
    )
    noizzen = _create_audio_input_filter(
        _configs(provider="noizzen", model="noise_cancellation", enhancement_level=0.3),
        TRANSPORT_TYPE_TELEPHONY,
    )
    assert isinstance(aic, NoizzenFilter) and isinstance(noizzen, NoizzenFilter)
    assert aic._mix == filter_module.DEFAULT_LEVEL
    assert noizzen._mix == 0.3


def test_unprovisioned_model_means_no_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(transport.static, "BREEZE_BUDDY_NOISE_MODEL_DIR", str(tmp_path))
    cfg = _configs(provider="noizzen", model="noise_cancellation")
    assert _create_audio_input_filter(cfg, TRANSPORT_TYPE_TELEPHONY) is None


def test_disabled_filter_is_none(provisioned):
    cfg = cast(
        ConfigurationModel,
        SimpleNamespace(noise_filter=NoiseFilterConfig(enable=False)),
    )
    assert _create_audio_input_filter(cfg, TRANSPORT_TYPE_TELEPHONY) is None


def test_template_accepts_noizzen_and_stored_aic_values():
    assert (
        NoiseFilterConfig(
            enable=True, provider="noizzen", model="noise_cancellation"
        ).provider
        == NoiseFilterProvider.NOIZZEN
    )
    assert NoiseFilterConfig(
        enable=True, provider="aic", model="noise_cancellation_s"
    ).model == (NoiseFilterModel.NOISE_CANCELLATION_S)
    assert NoiseFilterConfig(enable=True, type="aic").provider is None


def test_template_rejects_unknown_model():
    with pytest.raises(ValidationError):
        NoiseFilterConfig(enable=True, provider="noizzen", model="noise_cancellation_m")


# --- AIC path: templates with provider "aic" or a legacy `type` ---


@pytest.fixture
def aic_ready(monkeypatch, tmp_path, provisioned):
    """An AIC license key and every quail artifact present."""
    monkeypatch.setattr(transport.static, "BREEZE_BUDDY_AIC_LICENSE_KEY", "key")
    for attr in (
        "AIC_MODEL_PATH",
        "AIC_MODEL_PATH_16KHZ",
        "AIC_MODEL_PATH_S",
        "AIC_MODEL_PATH_S_16KHZ",
        "AIC_VOICE_FOCUS_MODEL_PATH",
    ):
        path = tmp_path / f"{attr}.aicmodel"
        path.write_bytes(b"aic")
        monkeypatch.setattr(transport.static, attr, str(path))


@pytest.mark.parametrize("shape", STORED_SHAPES[1:])  # every "aic" shape
def test_aic_templates_get_ai_coustics(aic_ready, shape):
    f = _create_audio_input_filter(_configs(**shape), TRANSPORT_TYPE_TELEPHONY)
    assert isinstance(f, _FailOpenAICFilter)


def test_noizzen_templates_stay_on_noizzen_when_ai_coustics_is_available(aic_ready):
    cfg = _configs(provider="noizzen", model="noise_cancellation")
    f = _create_audio_input_filter(cfg, TRANSPORT_TYPE_TELEPHONY)
    assert isinstance(f, NoizzenFilter)


def _noizzen_fallback():
    return NoizzenFilter(model="dpdfnet2_8k", model_path=Path("unused"))


def _aic(fallback=_noizzen_fallback) -> _FailOpenAICFilter:
    return _FailOpenAICFilter(
        fallback=fallback, license_key="k", model_path=Path("/nonexistent")
    )


async def test_refused_aic_key_at_start_hands_the_call_to_noizzen(
    fake_load, monkeypatch
):
    async def refused(self, sample_rate):
        self._aic_ready = False  # what AICFilter does when the SDK rejects the key

    monkeypatch.setattr(AICFilter, "start", refused)
    f = _aic()
    await f.start(8000)
    assert isinstance(f._fallback, NoizzenFilter)
    pcm = _pcm(_speechlike(0.2))
    out = b"".join(await _feed(f, pcm))
    assert 0 < len(out) <= len(pcm)


async def test_aic_failing_at_start_is_contained(fake_load, monkeypatch):
    async def corrupt(self, sample_rate):
        raise RuntimeError("model file corrupt")

    monkeypatch.setattr(AICFilter, "start", corrupt)
    f = _aic()
    await f.start(8000)
    assert isinstance(f._fallback, NoizzenFilter)


async def test_aic_error_mid_call_switches_to_noizzen(fake_load, monkeypatch):
    async def ok_start(self, sample_rate):
        self._aic_ready = True

    async def refused(self, audio):
        raise RuntimeError("SDK key was not authorized")

    monkeypatch.setattr(AICFilter, "start", ok_start)
    monkeypatch.setattr(AICFilter, "filter", refused)
    f = _aic()
    await f.start(8000)
    chunk = _pcm(_speechlike(0.02))
    assert await f.filter(chunk) == chunk  # this frame passes through
    assert isinstance(f._fallback, NoizzenFilter)  # the rest is filtered by Noizzen


async def test_aic_failure_without_a_fallback_passes_audio_through(monkeypatch):
    async def refused(self, sample_rate):
        self._aic_ready = False

    monkeypatch.setattr(AICFilter, "start", refused)
    f = _aic(fallback=lambda: None)
    await f.start(8000)
    chunk = _pcm(_speechlike(0.02))
    assert await f.filter(chunk) == chunk


async def test_aic_failing_mid_call_hands_back_the_audio_it_was_holding():
    class Processor:
        calls = 0

        async def process_async(self, block):
            Processor.calls += 1
            if Processor.calls > 1:
                raise RuntimeError("SDK key was not authorized")
            return block.copy()

    f = _aic(fallback=lambda: None)
    f._processor, f._aic_ready, f._sample_rate = Processor(), True, 8000
    f._frames_per_block = 80
    f._in_f32 = np.zeros((1, 80), dtype=np.float32)
    f._out_i16 = np.zeros(80, dtype=np.int16)
    held = _pcm(_speechlike(0.005))  # half a block: buffered, not yet processed
    assert await f.filter(held) == b""
    chunk = _pcm(_speechlike(0.02))
    assert await f.filter(chunk) == held + chunk  # the second block fails
    assert not f._audio_buffer


async def test_aic_cleanup_failing_at_call_end_is_contained():
    class Context:
        def reset(self):
            raise RuntimeError("SDK key was not authorized")

    stopped = []

    async def stop_fallback():
        stopped.append(True)

    f = _aic()
    f._processor_ctx = Context()
    f._fallback = cast(Any, SimpleNamespace(stop=stop_fallback))
    await f.stop()  # must not raise
    await f.stop()  # the transport can stop it again
    assert f._processor_ctx is None
    assert len(stopped) == 2


def _native_handle(freed_on: list):
    class NativeHandle:
        def __del__(self):
            freed_on.append(threading.current_thread() is threading.main_thread())

    return NativeHandle()


async def test_aic_teardown_runs_off_the_event_loop():
    freed_on: list = []
    f = _aic()
    f._processor = _native_handle(freed_on)
    await f.stop()
    assert freed_on == [False]


def test_aic_filter_still_has_what_the_wrapper_reads():
    """The private AICFilter state _FailOpenAICFilter reads, unchanged in pipecat
    1.1.0-1.15.0. If an upgrade fails this, the wrapper still never raises, but it
    can no longer see a refused key at start or hand back held audio: update it."""
    f = AICFilter(license_key="key", model_path=Path("model.aicmodel"))
    assert f._aic_ready is False
    assert isinstance(f._audio_buffer, bytearray)
    assert f._processor is None and f._processor_ctx is None


async def test_aic_filter_passes_audio_through_while_not_ready():
    """What the wrapper relies on once it marks AIC not ready without a fallback."""
    f = AICFilter(license_key="key", model_path=Path("model.aicmodel"))
    assert await f.filter(b"\x01\x02\x03\x04") == b"\x01\x02\x03\x04"


async def test_aic_stop_works_without_vad_ctx():
    """pipecat 1.8.0 dropped AICFilter._vad_ctx: stopping must neither raise nor
    free the handles on the event loop."""
    freed_on: list = []
    f = _aic()
    if hasattr(f, "_vad_ctx"):
        del f._vad_ctx  # as on pipecat 1.8 and later
    f._processor = _native_handle(freed_on)
    await f.stop()
    assert freed_on == [False]


async def test_control_frames_reach_noizzen_after_the_fallback(fake_load, monkeypatch):
    async def refused(self, sample_rate):
        self._aic_ready = False

    monkeypatch.setattr(AICFilter, "start", refused)
    f = _aic()
    await f.start(8000)
    noizzen = f._fallback
    assert isinstance(noizzen, NoizzenFilter)
    await f.process_frame(FilterEnableFrame(enable=False))
    assert noizzen._enabled is False


async def test_aic_is_released_when_the_call_moves_to_noizzen(fake_load, monkeypatch):
    """A mid-call failure ends the AIC session then, not at call end, and still
    frees it off the event loop; the call end frees nothing twice."""
    freed_on: list = []

    async def ok_start(self, sample_rate):
        self._aic_ready, self._processor = True, _native_handle(freed_on)

    async def refused(self, audio):
        raise RuntimeError("SDK key was not authorized")

    monkeypatch.setattr(AICFilter, "start", ok_start)
    monkeypatch.setattr(AICFilter, "filter", refused)
    f = _aic()
    await f.start(8000)
    await f.filter(_pcm(_speechlike(0.02)))
    assert isinstance(f._fallback, NoizzenFilter)
    assert f._release is not None
    await f._release  # runs in the background
    assert freed_on == [False] and f._processor is None
    await f.stop()
    assert freed_on == [False]


@pytest.mark.parametrize("slow", ["native teardown", "session end"])
async def test_a_mid_call_aic_failure_does_not_hold_up_the_audio(
    fake_load, monkeypatch, slow
):
    """AIC's teardown can wait seconds on its usage report (a native destructor on
    pipecat 1.1, an awaited session end on 1.8+). The caller's audio must not wait:
    Noizzen takes over at once, and the call end waits for the teardown instead."""
    freed: list = []

    class SlowHandle:
        def __del__(self):
            if slow == "native teardown":
                time.sleep(1.0)
            freed.append(True)

    original_stop = AICFilter.stop

    async def slow_stop(self):
        if slow == "session end":
            await asyncio.sleep(1.0)
        await original_stop(self)

    async def ok_start(self, sample_rate):
        self._aic_ready, self._processor = True, SlowHandle()

    async def refused(self, audio):
        raise RuntimeError("failed to report usage. Check your internet connection")

    monkeypatch.setattr(AICFilter, "start", ok_start)
    monkeypatch.setattr(AICFilter, "filter", refused)
    monkeypatch.setattr(AICFilter, "stop", slow_stop)
    f = _aic()
    await f.start(8000)
    chunk = _pcm(_speechlike(0.02))
    began = time.perf_counter()
    assert await f.filter(chunk) == chunk
    assert time.perf_counter() - began < 0.5  # not the 1 s teardown
    assert isinstance(f._fallback, NoizzenFilter)
    assert (
        sum(map(len, await _feed(f, _pcm(_speechlike(0.1))))) > 0
    )  # Noizzen meanwhile
    await f.stop()
    assert freed == [True]
