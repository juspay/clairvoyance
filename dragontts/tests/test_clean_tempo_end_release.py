"""End release — wired into the v3 chain for _clean_tempo only.

Every v3 clip, the initial greeting included (Breeze Buddy synthesizes it via
/tts/bytes, which is this synth path), goes hygiene -> release -> atempo.

Pinned behaviors:
- _clean_tempo releases the final syllable (quieter just before the voice
  ends than the same render without the release);
- the release never changes timing (same length with and without it);
- _tempo and the base model are untouched by it (byte-identical to a render
  with the release disabled).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import settings
from app.providers.elevenlabs import ElevenLabsProvider
from tests.test_atempo import (
    V3CONV,
    _install_fake_connect,
    _NoHTTPClient,
    _synth_v3conv,
    needs_ffmpeg,
)
from tests.test_elevenlabs_v3 import BASE, FakeConnect

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


def _voice_end_rms(pcm: bytes, reference: bytes) -> float:
    """RMS of the 20 ms before the voice ends, located on ``reference`` so a
    released and an unreleased render are compared at the same moment."""
    ref = np.frombuffer(reference, dtype="<i2").astype(float)
    x = np.frombuffer(pcm, dtype="<i2").astype(float)
    voice_end = np.flatnonzero(np.abs(ref) > 0.3 * np.abs(ref).max())[-1]
    seg = x[voice_end - RATE // 50 : voice_end]
    return float(np.sqrt(np.mean(seg**2)))


@needs_ffmpeg
async def test_clean_tempo_releases_the_final_syllable(monkeypatch):
    released = await _render(monkeypatch, V3CONV + "_clean_tempo")
    hard = await _render(monkeypatch, V3CONV + "_clean_tempo", release=False)
    assert _voice_end_rms(released, hard) < 0.7 * _voice_end_rms(hard, hard)


@needs_ffmpeg
async def test_release_never_changes_timing(monkeypatch):
    released = await _render(monkeypatch, V3CONV + "_clean_tempo")
    hard = await _render(monkeypatch, V3CONV + "_clean_tempo", release=False)
    assert len(released) == len(hard)


@needs_ffmpeg
@pytest.mark.parametrize("model", [V3CONV + "_tempo", V3CONV])
async def test_other_variants_are_untouched(monkeypatch, model):
    with_release = await _render(monkeypatch, model)
    without = await _render(monkeypatch, model, release=False)
    assert with_release == without
