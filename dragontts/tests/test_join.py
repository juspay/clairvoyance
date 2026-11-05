"""Per-sentence level and timbre (clean_tempo_v2) — one static adjustment per clip.

Pinned behaviors:
- even_level lands a clip's speech level on the target within the cap, never
  clips, and leaves silent clips alone;
- the loudness contour inside a clip is preserved (it moves as a whole);
- two takes of different loudness end up at the same level (the join fix);
- brief spikes are limited locally so a quiet filler still reaches the target;
- match_tilt moves the band balance toward the target and preserves RMS;
- the vectorized limiter equals the per-sample loop definition, and stays
  fast when voiced peaks put thousands of samples over the ceiling (it
  runs on the event loop).
"""

from __future__ import annotations

import numpy as np

from app.audio.join import even_level, match_tilt, spectral_tilt_db, speech_level_dbfs

RATE = 44100


def _speech(seconds: float, amp: float, seed: int = 7, bright: float = 0.0) -> bytes:
    """Amplitude-modulated 'speech' with a stress contour; ``bright`` adds a
    2.5 kHz component to shift the spectral tilt."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * seconds)) / RATE
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 2.5 * t) ** 2  # syllable-ish up/down
    x = (
        amp
        * env
        * (np.sin(2 * np.pi * 220 * t) + bright * np.sin(2 * np.pi * 2500 * t))
    )
    return (x + rng.normal(0, 30, len(t))).astype("<i2").tobytes()


def _frames_db(pcm: bytes) -> np.ndarray:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    f = RATE // 100
    m = len(x) // f
    return 20 * np.log10(np.sqrt(np.mean(x[: m * f].reshape(m, f) ** 2, axis=1)) + 1e-9)


# -- even_level: one gain per clip -------------------------------------------


def test_even_level_hits_target():
    # ~-18 dBFS speech: needs +5 dB, inside the cap
    out = even_level(_speech(1.0, 8000), RATE, target_dbfs=-13.0)
    assert abs(speech_level_dbfs(out, RATE) - (-13.0)) < 0.5


def test_gain_is_capped():
    quiet = _speech(1.0, 1500)  # ~-32 dBFS: needs ~+19 dB, capped at +6
    out = even_level(quiet, RATE, target_dbfs=-13.0, max_gain_db=6.0)
    assert (
        abs((speech_level_dbfs(out, RATE) - speech_level_dbfs(quiet, RATE)) - 6.0) < 0.3
    )


def test_never_clips():
    loud = _speech(1.0, 26000)
    out = even_level(loud, RATE, target_dbfs=-3.0, max_gain_db=6.0)
    y = np.frombuffer(out, dtype="<i2").astype(np.int32)
    assert np.max(np.abs(y)) <= int(32768 * 10 ** (-1.0 / 20)) + 1


def test_contour_inside_the_sentence_is_preserved():
    clip = _speech(1.0, 3000)
    out = even_level(clip, RATE, target_dbfs=-13.0)
    before, after = _frames_db(clip), _frames_db(out)
    shift = np.median(after - before)
    assert np.allclose(after - before, shift, atol=0.1)  # one gain, no reshaping


def test_two_takes_end_up_at_one_level():
    a = even_level(_speech(1.0, 8000, seed=1), RATE, target_dbfs=-13.0)
    b = even_level(_speech(1.0, 11000, seed=2), RATE, target_dbfs=-13.0)
    assert abs(speech_level_dbfs(a, RATE) - speech_level_dbfs(b, RATE)) < 0.5


def test_silent_clip_untouched():
    silent = np.zeros(RATE, dtype="<i2").tobytes()
    assert even_level(silent, RATE, target_dbfs=-13.0) == silent


# -- match_tilt --------------------------------------------------------------


def test_match_tilt_moves_toward_target_and_keeps_rms():
    dark = _speech(1.0, 5000, bright=0.05)
    bright = _speech(1.0, 5000, bright=0.6)
    target = spectral_tilt_db(dark, RATE)
    out = match_tilt(bright, RATE, target_db=target, max_db=4.0)
    assert abs(spectral_tilt_db(out, RATE) - target) < abs(
        spectral_tilt_db(bright, RATE) - target
    )
    x = np.frombuffer(bright, dtype="<i2").astype(float)
    y = np.frombuffer(out, dtype="<i2").astype(float)
    assert abs(np.sqrt(np.mean(y**2)) / np.sqrt(np.mean(x**2)) - 1) < 0.02
    assert len(out) == len(bright)


# -- spike limiter -----------------------------------------------------------


def _quiet_with_spike() -> bytes:
    """A short filler like v3's "हाँजी.": ~-20 dBFS speech (needs ~+4 dB) with one
    2 ms near-full-scale spike that leaves no headroom for that gain."""
    x = np.frombuffer(_speech(0.8, 5500, seed=5), dtype="<i2").astype(np.float64).copy()
    i = int(0.4 * RATE)
    x[i : i + int(0.002 * RATE)] = 32000.0
    return x.astype("<i2").tobytes()


def test_spike_limiter_lets_a_quiet_filler_reach_the_target():
    clip = _quiet_with_spike()
    out = even_level(clip, RATE, target_dbfs=-16.0)
    assert abs(speech_level_dbfs(out, RATE) - (-16.0)) < 0.5
    y = np.frombuffer(out, dtype="<i2").astype(np.int32)
    assert np.max(np.abs(y)) <= int(32768 * 10 ** (-1.0 / 20)) + 1  # never clips


def test_spike_limiter_is_local():
    clip = _quiet_with_spike()
    out = even_level(clip, RATE, target_dbfs=-16.0)
    x = np.frombuffer(clip, dtype="<i2").astype(np.float64)
    y = np.frombuffer(out, dtype="<i2").astype(np.float64)
    ratio = np.divide(y, x, out=np.full_like(x, np.nan), where=np.abs(x) > 200)
    gain = np.nanmedian(ratio)
    touched = np.abs(ratio - gain) > 0.02 * gain
    # only the ~200 ms around the 2 ms spike differs from the one clip gain
    assert np.nansum(touched) / np.sum(np.abs(x) > 200) < 0.25


def test_limiter_off_falls_back_to_headroom():
    clip = _quiet_with_spike()
    out = even_level(clip, RATE, target_dbfs=-16.0, max_limit_db=0.0)
    assert speech_level_dbfs(out, RATE) < -18.0  # held back by the spike


# -- vectorized limiter == the per-sample loop it replaced -------------------


def _spike_gain_reference(y, sample_rate, ceiling, hold_ms, release_ms):
    """The original per-sample loop version, kept as the definition the
    vectorized limiter must reproduce."""
    over = np.flatnonzero(np.abs(y) > ceiling)
    g = np.ones_like(y)
    if len(over) == 0:
        return g
    need = ceiling / np.abs(y[over])
    hold = max(1, int(sample_rate * hold_ms / 1000))
    for i, v in zip(over, need):
        lo, hi = max(0, i - hold), min(len(y), i + hold + 1)
        np.minimum(g[lo:hi], v, out=g[lo:hi])
    tau = max(1.0, sample_rate * release_ms / 1000)
    coeff = 1.0 - np.exp(-1.0 / tau)
    reach = int(6 * tau) + hold
    regions = []
    for a, b in zip(np.maximum(over - reach, 0), np.minimum(over + reach + 1, len(y))):
        if regions and a <= regions[-1][1]:
            regions[-1][1] = max(regions[-1][1], b)
        else:
            regions.append([a, b])
    for a, b in regions:
        seg = g[a:b]
        fwd = seg.copy()
        for n in range(1, len(fwd)):
            if fwd[n] > fwd[n - 1]:
                fwd[n] = min(fwd[n], fwd[n - 1] + (1.0 - fwd[n - 1]) * coeff)
        bwd = seg.copy()
        for n in range(len(bwd) - 2, -1, -1):
            if bwd[n] > bwd[n + 1]:
                bwd[n] = min(bwd[n], bwd[n + 1] + (1.0 - bwd[n + 1]) * coeff)
        g[a:b] = np.minimum(fwd, bwd)
    return g


def _dense_peaks(seconds: float) -> np.ndarray:
    """Voiced-speech worst case: over-ceiling pitch peaks every ~6.7 ms, with
    quiet gaps so regions both merge and split, plus spikes at both edges."""
    rng = np.random.default_rng(11)
    n = int(RATE * seconds)
    y = rng.normal(0, 1500, n)
    peaks = np.arange(0, n, RATE // 150)
    y[peaks] = rng.uniform(29500, 32767, len(peaks)) * rng.choice([-1, 1], len(peaks))
    y[int(0.4 * n) : int(0.6 * n)] *= 0.05  # a pause: two separate regions
    y[[0, n - 1]] = 32000
    return y


def test_vectorized_limiter_matches_the_loop_definition():
    from app.audio.join import _spike_gain

    ceiling = 32768 * 10 ** (-1 / 20)
    for y in (_dense_peaks(0.5), np.frombuffer(_quiet_with_spike(), "<i2") * 1.8):
        want = _spike_gain_reference(y, RATE, ceiling, 1.5, 10.0)
        got = _spike_gain(y, RATE, ceiling, 1.5, 10.0)
        assert np.allclose(got, want, rtol=0, atol=1e-9)
        assert np.all(np.abs(y * got) <= ceiling + 1e-6)  # never over the ceiling


def test_limiter_is_fast_on_dense_peaks():
    import time

    from app.audio.join import _spike_gain

    y = _dense_peaks(8.0)
    t = time.perf_counter()
    _spike_gain(y, RATE, 32768 * 10 ** (-1 / 20), 1.5, 10.0)
    # the per-sample loop took ~150 ms here; vectorized it is a few ms
    assert time.perf_counter() - t < 0.05
