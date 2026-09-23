"""soften_end — the v3 end release.

Pinned behaviors (v3 ends every clip with its final syllable dropping ~13 dB
inside 10 ms, heard as the voice being cut off):
- only the voice's last ``release_ms`` changes; everything before it is
  byte-identical and the clip length never changes;
- the voice ends at ``-depth_db``, along a curve (no step);
- no sample ever gets louder;
- the air after the voice fades on down to zero (no step from room tone into
  digital silence);
- silent, too-short and ``release_ms=0`` clips are returned unchanged;
- a voice shorter than the release window is released over what there is;
- it works at every rate the chain runs at (8, 16, 44.1 kHz).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.level import soften_end

RATE = 44100


def _voice(seconds: float, rate: int = RATE, amp: float = 6000.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype("<i2")


def _clip(
    voice_s: float = 1.0, air_s: float = 0.2, rate: int = RATE, noise: float = 0.0
) -> tuple[bytes, int]:
    """Voice followed by trailing air; returns the clip and the voice's
    length in samples."""
    voice = _voice(voice_s, rate)
    air = np.random.default_rng(1).normal(0, noise, int(rate * air_s)).astype("<i2")
    return np.concatenate([voice, air]).tobytes(), len(voice)


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)))


# -- the release -------------------------------------------------------------


def test_only_the_voice_ending_changes():
    clip, voice_n = _clip()
    out, x = _samples(soften_end(clip, RATE, release_ms=90)), _samples(clip)
    untouched = voice_n - int(0.1 * RATE)  # before the 90 ms window (+ a frame)
    assert np.array_equal(out[:untouched], x[:untouched])
    assert len(out) == len(x)


def test_voice_ends_at_the_release_depth():
    clip, voice_n = _clip()
    out = _samples(soften_end(clip, RATE, release_ms=90, depth_db=14))
    x = _samples(clip)
    last_5ms = slice(voice_n - int(0.005 * RATE), voice_n)
    ratio = _rms(out[last_5ms]) / _rms(x[last_5ms])
    assert 10 ** (-16 / 20) < ratio < 10 ** (-12 / 20)  # ~-14 dB


def test_release_is_a_curve_not_a_step():
    clip, voice_n = _clip()
    out, x = _samples(soften_end(clip, RATE, release_ms=90)), _samples(clip)
    hop = RATE // 200  # 5 ms
    start = voice_n - int(0.09 * RATE)
    ratios = [
        _rms(out[i : i + hop]) / _rms(x[i : i + hop])
        for i in range(start, voice_n - hop, hop)
    ]
    assert all(b <= a + 1e-3 for a, b in zip(ratios, ratios[1:]))  # only goes down
    assert max(a - b for a, b in zip(ratios, ratios[1:])) < 0.15  # no jump


def test_no_sample_gets_louder():
    clip, _ = _clip(noise=60.0)
    out, x = _samples(soften_end(clip, RATE)), _samples(clip)
    assert np.all(np.abs(out) <= np.abs(x))


def test_air_after_the_voice_fades_to_zero():
    clip, voice_n = _clip(noise=60.0)
    out = _samples(soften_end(clip, RATE))
    assert abs(out[-1]) <= 1
    air = out[voice_n + int(0.01 * RATE) :]
    assert _rms(air[-int(0.02 * RATE) :]) < _rms(air[: int(0.02 * RATE)])


# -- no-ops and edges --------------------------------------------------------


def test_silence_is_unchanged():
    silent = np.zeros(RATE, dtype="<i2").tobytes()
    assert soften_end(silent, RATE) == silent


def test_too_short_clip_is_unchanged():
    tiny = _voice(0.04).tobytes()  # 8 frames of 5 ms: under the 10-frame floor
    assert soften_end(tiny, RATE) == tiny


def test_release_zero_is_off():
    clip, _ = _clip()
    assert soften_end(clip, RATE, release_ms=0) == clip


def test_voice_shorter_than_the_release_window():
    clip, voice_n = _clip(voice_s=0.06, air_s=0.05)
    out, x = _samples(soften_end(clip, RATE, release_ms=90)), _samples(clip)
    assert len(out) == len(x)
    last_5ms = slice(voice_n - int(0.005 * RATE), voice_n)
    assert _rms(out[last_5ms]) < 0.3 * _rms(x[last_5ms])


@pytest.mark.parametrize("rate", [8000, 16000, 44100])
def test_works_at_every_chain_rate(rate):
    clip, voice_n = _clip(rate=rate)
    out, x = _samples(soften_end(clip, rate)), _samples(clip)
    untouched = voice_n - int(0.1 * rate)
    assert np.array_equal(out[:untouched], x[:untouched])
    last_5ms = slice(voice_n - int(0.005 * rate), voice_n)
    assert _rms(out[last_5ms]) < 0.3 * _rms(x[last_5ms])
