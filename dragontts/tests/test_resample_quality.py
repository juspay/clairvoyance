"""Quality pins for the anti-aliased serve-time resampler (format.py).

Why this module exists: every telephony serve downsamples provider-native
audio to 8 kHz mu-law. The previous unfiltered conversion (audioop.ratecv
linear interpolation) folds out-of-band energy INTO the 0-4 kHz voice band —
a 6 kHz tone aliases down at ~94% amplitude, and atempo stretch artifacts
generated above 4 kHz land straight in the speech. The Hann-windowed-sinc
kernel must reject the out-of-band content while leaving in-band speech
untouched. These tests pin both sides of that contract at every rate pair the
pipeline can produce (44.1k/24k/16k native -> 8k telephony).
"""

from __future__ import annotations

import audioop

import numpy as np
import pytest

from app.audio.format import _resample_pcm, convert_audio

# audioop is only imported to demonstrate the aliasing baseline we replaced.


def _sine(freq: float, rate: int, seconds: float, amp: float = 12000.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def _rms(data: bytes) -> float:
    x = np.frombuffer(data, dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


@pytest.mark.parametrize("in_rate", [44100, 24000, 16000])
def test_out_of_band_tone_is_rejected_not_aliased(in_rate):
    # 6 kHz is above the 8 kHz output Nyquist: unfiltered interpolation lets
    # it alias back at ~94% amplitude (see the ratecv baseline below); the
    # sinc kernel must keep it under 5% (-26 dB), i.e. inaudible under speech
    # and below the mu-law noise floor.
    src = _sine(6000, in_rate, 2.0)
    out = _resample_pcm(src, in_rate, 8000)
    assert _rms(out) < 0.05 * _rms(src)
    aliased = audioop.ratecv(src, 2, 1, in_rate, 8000, None)[0]
    assert _rms(aliased) > 0.5 * _rms(src)  # the baseline really did alias


def test_in_band_tone_level_preserved():
    src = _sine(1000, 44100, 2.0)
    out = _resample_pcm(src, 44100, 8000)
    assert _rms(out) == pytest.approx(_rms(src), rel=0.02)


@pytest.mark.parametrize("freq", [2000, 2500, 3000])
def test_presence_band_preserved(freq):
    # Regression: the kernel was missing its factor of 2 (sinc(fc*d) instead
    # of sinc(2*fc*d)), which rolled the real cutoff down to ~1.8 kHz — the
    # voice's 2-4 kHz presence band was attenuated 14-32 dB and every call
    # sounded muffled. 1 kHz and 6 kHz tests both passed; this band was the
    # blind spot.
    src = _sine(freq, 44100, 2.0)
    out = _resample_pcm(src, 44100, 8000)
    assert _rms(out) == pytest.approx(_rms(src), rel=0.05)


def test_dc_gain_is_unity():
    src = (9000.0 * np.ones(44100)).astype("<i2").tobytes()
    out = _resample_pcm(src, 44100, 8000)
    assert np.frombuffer(out, dtype="<i2").mean() == pytest.approx(9000.0, abs=1.0)


def test_output_length_scales_with_rate():
    out = _resample_pcm(_sine(1000, 44100, 3.0), 44100, 8000)
    assert len(out) / 2 == pytest.approx(3.0 * 8000, rel=0.01)


def test_same_rate_is_byte_passthrough():
    src = _sine(1000, 44100, 1.0)
    assert _resample_pcm(src, 44100, 44100) == src


def test_upsample_preserves_tone_and_length():
    out = _resample_pcm(_sine(1000, 8000, 2.0), 8000, 44100)
    assert len(out) / 2 == pytest.approx(2.0 * 44100, rel=0.01)
    assert _rms(out) == pytest.approx(_rms(_sine(1000, 8000, 2.0)), rel=0.02)


def test_telephony_chain_full_convert_to_mulaw():
    # The exact Clairvoyance serve: pcm_s16le@44100 native -> mulaw@8000.
    # One byte per sample at 8k, and after decoding back the in-band tone
    # survives while the out-of-band tone is gone.
    src = _sine(1000, 44100, 2.0)
    ulaw = convert_audio(
        src,
        native_encoding="pcm_s16le",
        native_rate=44100,
        out_encoding="mulaw",
        out_rate=8000,
    )
    assert len(ulaw) == pytest.approx(2.0 * 8000, rel=0.01)
    decoded = audioop.ulaw2lin(ulaw, 2)
    assert _rms(decoded) > 0.5 * _rms(src)

    src_hi = _sine(6000, 44100, 2.0)
    ulaw_hi = convert_audio(
        src_hi,
        native_encoding="pcm_s16le",
        native_rate=44100,
        out_encoding="mulaw",
        out_rate=8000,
    )
    assert _rms(audioop.ulaw2lin(ulaw_hi, 2)) < 0.08 * _rms(src_hi)
