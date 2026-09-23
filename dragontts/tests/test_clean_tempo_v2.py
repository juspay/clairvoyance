"""eleven_v3_conversational_clean_tempo_v2 — the join-tuned chain.

Pinned behaviors:
- the v2 suffix is its own variant, matched before _clean_tempo / _tempo
  (longest first), and stripped to the base id before any upstream call;
- v2 generates full-band like _clean_tempo;
- _clean_tempo is untouched by v2: its 120 ms tail and its level stay, and
  the v2 join chain never runs for it;
- v2 keeps a 240 ms tail, lands each sentence on the level target, and
  releases the final syllable;
- the v2 join chain runs exactly once per v2 synth;
- the timbre match is off unless the voice is listed in
  elevenlabs_v2_timbre_targets_db.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.join import speech_level_dbfs
from app.core.config import settings
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import (
    normalize_v3_conversational,
    v3_conversational_variant,
)
from tests.test_atempo import (
    V3CONV,
    _install_fake_connect,
    _NoHTTPClient,
    _synth_v3conv,
    needs_ffmpeg,
)
from tests.test_elevenlabs_v3 import BASE, VOICE, FakeConnect

V2 = V3CONV + "_clean_tempo_v2"
CLEAN = V3CONV + "_clean_tempo"
RATE = 44100


def _speech_with_pads(amp: float = 4000.0) -> bytes:
    """1 s of syllable-shaped voice between 400 ms pads, as v3 returns it."""
    t = np.arange(RATE) / RATE
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 2.5 * t) ** 2
    voice = (amp * env * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()
    pad = b"\x00\x00" * int(0.4 * RATE)
    return pad + voice + pad


async def _render(monkeypatch, model: str, *, release: bool = True) -> bytes:
    """One synth through the real v3 chain against a fake ElevenLabs socket."""
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", RATE)
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    if not release:
        monkeypatch.setattr("app.providers.elevenlabs.soften_end", lambda a, _sr: a)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(
        provider, {"tempo": 2.0}, connect, _speech_with_pads(), model=model
    )
    return result.audio


def _seconds(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE


def _voice_end_rms(pcm: bytes, reference: bytes) -> float:
    """RMS of the 20 ms before the voice ends, located on ``reference`` so a
    released and an unreleased render are compared at the same moment."""
    ref = np.frombuffer(reference, dtype="<i2").astype(float)
    x = np.frombuffer(pcm, dtype="<i2").astype(float)
    voice_end = np.flatnonzero(np.abs(ref) > 0.3 * np.abs(ref).max())[-1]
    seg = x[voice_end - RATE // 50 : voice_end]
    return float(np.sqrt(np.mean(seg**2)))


def _spy(monkeypatch, target: str) -> list:
    """Record calls to ``target`` while still running it."""
    module, name = target.rsplit(".", 1)
    real = getattr(__import__(module, fromlist=[name]), name)
    calls: list = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(target, spy)
    return calls


# -- model id ----------------------------------------------------------------


@pytest.mark.parametrize(
    "model, variant",
    [(V2, "clean_tempo_v2"), (CLEAN, "clean_tempo"), (V3CONV + "_tempo", "tempo")],
)
def test_suffixes_map_to_their_own_variant(model, variant):
    assert v3_conversational_variant(model) == variant
    assert normalize_v3_conversational(model) == V3CONV  # upstream sees the base


def test_v2_is_full_band(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", RATE)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    assert provider.synth_native_format(V2, {"tempo": 1.09}) == ("pcm_s16le", RATE)


# -- _clean_tempo is untouched -------------------------------------------------


@needs_ffmpeg
async def test_clean_tempo_keeps_its_tail_and_level(monkeypatch):
    audio = await _render(monkeypatch, CLEAN)
    # speech + 60/120 ms caps, halved by tempo 2.0; level untouched (~-21 dBFS)
    assert _seconds(audio) == pytest.approx((1.0 + 0.18) / 2.0, rel=0.1)
    assert speech_level_dbfs(audio, RATE) < -18.0


@needs_ffmpeg
async def test_join_chain_never_runs_for_clean_tempo(monkeypatch):
    calls = _spy(monkeypatch, "app.audio.join.even_level")
    await _render(monkeypatch, CLEAN)
    assert calls == []


# -- the v2 chain --------------------------------------------------------------


@needs_ffmpeg
async def test_v2_keeps_the_longer_tail(monkeypatch):
    audio = await _render(monkeypatch, V2)
    # speech + 60/240 ms caps, halved by tempo 2.0
    assert _seconds(audio) == pytest.approx((1.0 + 0.30) / 2.0, rel=0.1)


@needs_ffmpeg
async def test_v2_lands_on_the_level_target(monkeypatch):
    v2 = await _render(monkeypatch, V2)
    plain = await _render(monkeypatch, CLEAN)
    target = settings.elevenlabs_v2_level_target_dbfs
    # atempo runs after the level step and shifts this tone by ~1 dB
    v2_off = abs(speech_level_dbfs(v2, RATE) - target)
    plain_off = abs(speech_level_dbfs(plain, RATE) - target)
    assert v2_off < 2.0 and v2_off < plain_off


@needs_ffmpeg
async def test_v2_releases_the_final_syllable(monkeypatch):
    released = await _render(monkeypatch, V2)
    hard = await _render(monkeypatch, V2, release=False)
    assert len(released) == len(hard)  # timing untouched
    assert _voice_end_rms(released, hard) < 0.7 * _voice_end_rms(hard, hard)


@needs_ffmpeg
async def test_join_chain_runs_once_per_v2_synth(monkeypatch):
    calls = _spy(monkeypatch, "app.audio.join.even_level")
    await _render(monkeypatch, V2)
    assert len(calls) == 1


# -- timbre match --------------------------------------------------------------


@needs_ffmpeg
async def test_timbre_match_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v2_timbre_targets_db", {})
    calls = _spy(monkeypatch, "app.audio.join.match_tilt")
    await _render(monkeypatch, V2)
    assert calls == []


@needs_ffmpeg
async def test_timbre_match_runs_for_a_listed_voice(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v2_timbre_targets_db", {VOICE: -3.0})
    calls = _spy(monkeypatch, "app.audio.join.match_tilt")
    await _render(monkeypatch, V2)
    assert len(calls) == 1 and calls[0][1]["target_db"] == -3.0
