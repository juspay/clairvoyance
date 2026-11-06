"""Utterance hygiene — dead-air trimming, pause capping, silence gating.

Pinned behaviors (measurements that motivated this are from production
44.1 kHz v3 clips):
- leading/trailing silence is trimmed to small caps (ElevenLabs averages
  ~300 ms trailing dead air per clip);
- internal pauses are capped (260-440 ms random pauses observed);
- retained silence (the capped pause + pads) is gated to digital silence
  (the v3 noise floor sits only ~22-27 dB under speech);
- speech frames pass through untouched, and all-loud clips are returned
  byte-identical (no-op safety);
- too-short clips are returned unchanged.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.hygiene import clean_utterance

RATE = 44100
FRAME_TOL = 0.045  # two 20ms frames of scheduling slack in every length check


def _speech(seconds: float, amp: float = 9000.0, seed: int = 7) -> bytes:
    """Loud 'speech': amplitude-modulated tone with realistic frame RMS."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * seconds)) / RATE
    env = 0.7 + 0.3 * np.sin(2 * np.pi * 3.0 * t)
    return (
        (amp * env * np.sin(2 * np.pi * 220 * t) + rng.normal(0, 50, len(t)))
        .astype("<i2")
        .tobytes()
    )


def _silence(seconds: float, noise: float = 500.0, seed: int = 3) -> bytes:
    """'Silence' at the measured v3 noise-floor level (~-24 dB under speech)."""
    rng = np.random.default_rng(seed)
    return rng.normal(0, noise, int(RATE * seconds)).astype("<i2").tobytes()


def _rms(data: bytes) -> float:
    x = np.frombuffer(data, dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def test_trims_leading_and_trailing_silence():
    clip = _silence(0.5) + _speech(1.0) + _silence(0.4)
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    assert len(out) / 2 / RATE == pytest.approx(1.0 + 0.060 + 0.080, abs=FRAME_TOL)


def test_caps_internal_pauses():
    clip = _speech(0.5) + _silence(0.6) + _speech(0.5)
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    assert len(out) / 2 / RATE == pytest.approx(0.5 + 0.250 + 0.5, abs=FRAME_TOL)


def test_gates_retained_silence_to_digital_silence():
    clip = (
        _silence(0.3, noise=300.0)
        + _speech(0.5)
        + _silence(0.6, noise=300.0)
        + _speech(0.5)
    )
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    # With the pause and pads gated to ~0, output RMS is dominated by speech
    # (a raw pause-heavy clip would sit far below the speech level).
    assert _rms(out) == pytest.approx(_rms(_speech(0.5)), rel=0.25)


def test_speech_level_preserved():
    speech = _speech(1.0)
    clip = _silence(0.2) + speech + _silence(0.3)
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    assert _rms(out) == pytest.approx(_rms(speech), rel=0.10)


def test_all_loud_clip_is_passthrough():
    clip = _speech(1.5)
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    assert out == clip


def test_too_short_clip_unchanged():
    clip = _speech(0.02)  # one frame
    assert clean_utterance(clip, RATE) == clip


def test_gate_floor_attenuates_but_keeps_room_tone():
    # floor 0.5 on a realistic noise floor (~400 RMS, below the speech
    # threshold): the retained pause must carry ~half the noise energy, not
    # digital zero (zeroing is what flattened the expression on calls).
    clip = _speech(0.5) + _silence(0.6, noise=400.0) + _speech(0.5)
    out = clean_utterance(
        clip, RATE, lead_ms=60, tail_ms=120, max_pause_ms=300, gate_floor=0.5
    )
    x = np.frombuffer(out, dtype="<i2").astype(np.float64)
    fr = 20 * RATE // 1000
    m = len(x) // fr
    rms = np.sqrt(np.mean(x[: m * fr].reshape(m, fr) ** 2, axis=1))
    quiet = np.sort(rms)[: max(1, m // 5)]
    quiet_level = float(np.sqrt(np.mean(quiet**2)))
    assert 0.2 * 400 < quiet_level < 0.8 * 400


# -- content-line dial -------------------------------------------------------
# Frames above max(p95*factor, abs) are content (never trimmed/capped/gated);
# frames below are silence/noise. DEFAULT (0.15 = the loud line) is the
# call-approved chain — byte-identical render to the approved comparisons.
# The 0.09 "protective" preset keeps quiet material down to ~-21 dB.


def _tone(seconds: float, amp: float) -> bytes:
    """Deterministic pure tone: rms = amp/sqrt(2), no amplitude modulation."""
    t = np.arange(int(RATE * seconds)) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()


def test_default_caps_quiet_pause_call_approved_semantics():
    # ~-18 dB under speech: BELOW the default content line (the loud line),
    # so it caps like the approved call chain did.
    clip = _speech(0.5) + _tone(0.6, 1100) + _speech(0.5)
    out = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    assert len(out) / 2 / RATE == pytest.approx(0.5 + 0.250 + 0.5, abs=FRAME_TOL)


def test_protective_preset_keeps_quiet_expressive_pause():
    # Same clip at content_factor=0.09: the ~-18 dB pause is content now —
    # never capped, never gated, full length + level.
    clip = _speech(0.5) + _tone(0.6, 1100) + _speech(0.5)
    out = clean_utterance(
        clip,
        RATE,
        lead_ms=60,
        tail_ms=80,
        max_pause_ms=250,
        content_factor=0.09,
        content_abs=120.0,
    )
    # The clip is content end-to-end (speech-quiet-speech), so nothing trims:
    # the pause must survive at FULL length, not be capped to 250 ms.
    assert len(out) / 2 / RATE == pytest.approx(0.5 + 0.6 + 0.5, abs=FRAME_TOL)
    # Untouched level in the middle of the quiet passage (past the gate
    # hangover, well inside the clip).
    mid = out[int((0.5 + 0.3) * RATE) * 2 : int((0.5 + 0.3) * RATE + 0.2 * RATE) * 2]
    assert _rms(mid) == pytest.approx(1100 / np.sqrt(2), rel=0.1)


def test_protective_preset_keeps_soft_tail():
    # A breathy tail louder than the protective content line survives the
    # trailing trim intact (at the default line it would trim at the last
    # loud frame).
    clip = _speech(0.6) + _tone(0.4, 1100) + _silence(0.3)
    out = clean_utterance(
        clip,
        RATE,
        lead_ms=60,
        tail_ms=80,
        max_pause_ms=250,
        content_factor=0.09,
        content_abs=120.0,
    )
    assert len(out) / 2 / RATE == pytest.approx(0.6 + 0.4 + 0.080, abs=FRAME_TOL)


def test_sub_floor_noise_still_trims_and_caps():
    # The measured floor (~-22 dB and below) remains fully cleanable at BOTH
    # dial settings: pads trim and long pauses cap.
    clip = _speech(0.5) + _silence(0.6, noise=300.0) + _speech(0.5)
    out_default = clean_utterance(clip, RATE, lead_ms=60, tail_ms=80, max_pause_ms=250)
    out_protective = clean_utterance(
        clip,
        RATE,
        lead_ms=60,
        tail_ms=80,
        max_pause_ms=250,
        content_factor=0.09,
        content_abs=120.0,
    )
    for out in (out_default, out_protective):
        assert len(out) / 2 / RATE == pytest.approx(0.5 + 0.250 + 0.5, abs=FRAME_TOL)
