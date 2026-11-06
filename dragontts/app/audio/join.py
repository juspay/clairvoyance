"""Per-sentence level and timbre — make separately generated clips join.

``eleven_v3_conversational_clean_tempo_v2`` only. Every cached v3 clip is its
own generation, so a call plays sentences that were synthesized independently,
often on different days. Measured on production takes of one voice: the same
sentence lands up to ~6 dB apart in loudness across takes, and consecutive
sentences of a turn jump ~2.5 dB (p90 ~6.4 dB) — heard as the voice getting
louder and softer between chunks.

Each is ONE adjustment per clip, applied before the clip is cached:

- :func:`even_level` — a single gain that moves the clip's speech level to a
  target. The loudness contour inside the sentence (stress, emphasis, the
  phrase-final dip) is untouched: it only moves up or down as a whole. The
  one exception is a brief near-full-scale spike (common on short v3 fillers
  like "हाँजी."), which is pressed down locally — a few ms, at most 6 dB,
  smooth recovery — so the clip can reach the target without clipping.
- :func:`match_tilt` — a single zero-phase spectral tilt that moves the clip's
  high/low band balance to a per-voice target. No pitch, timing or loudness
  contour is edited.
"""

from __future__ import annotations

import numpy as np

_FRAME_MS = 10
# Frames under this are silence/air, not speech, when measuring the level.
_SPEECH_FLOOR_DBFS = -45.0
_FULL_SCALE = 32768.0


def _frames_dbfs(x: np.ndarray, sample_rate: int) -> np.ndarray:
    frame = max(1, sample_rate * _FRAME_MS // 1000)
    m = len(x) // frame
    if m == 0:
        return np.array([])
    rms = np.sqrt(np.mean(x[: m * frame].reshape(m, frame) ** 2, axis=1))
    return 20 * np.log10(rms / _FULL_SCALE + 1e-12)


def speech_level_dbfs(pcm: bytes, sample_rate: int) -> float | None:
    """Median level of the clip's speech frames (dBFS), or None if silent."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    db = _frames_dbfs(x, sample_rate)
    speech = db[db > _SPEECH_FLOOR_DBFS]
    return float(np.median(speech)) if len(speech) >= 5 else None


def _window_min(x: np.ndarray, half: int) -> np.ndarray:
    """Minimum of ``x`` over ``[i - half, i + half]`` for every ``i`` (edges
    padded with 1.0, the no-reduction gain). Van Herk / Gil-Werman: block
    prefix/suffix minima, O(n) in numpy with no per-sample Python loop."""
    w = 2 * half + 1
    n = len(x)
    blocks = -(-(n + 2 * half) // w)
    pad = np.ones(blocks * w)
    pad[half : half + n] = x
    grid = pad.reshape(blocks, w)
    pre = np.minimum.accumulate(grid, axis=1).ravel()
    suf = np.minimum.accumulate(grid[:, ::-1], axis=1)[:, ::-1].ravel()
    return np.minimum(suf[:n], pre[w - 1 : w - 1 + n])


def _decay_max(x: np.ndarray, decay: float) -> np.ndarray:
    """``y[n] = max(x[n], decay * y[n - 1])`` for ``x >= 0``, vectorized.

    Unrolled, ``y[n] = max_k x[k] * decay**(n - k)``: in log space that is a
    running maximum, so one ``np.maximum.accumulate`` replaces the loop."""
    k = np.arange(len(x), dtype=np.float64)
    log_decay = np.log(decay)
    z = np.full(len(x), -np.inf)
    pos = x > 0
    z[pos] = np.log(x[pos]) - k[pos] * log_decay
    return np.exp(np.maximum.accumulate(z) + k * log_decay)


def _spike_gain(
    y: np.ndarray, sample_rate: int, ceiling: float, hold_ms: float, release_ms: float
) -> np.ndarray:
    """Per-sample gain (<= 1) that keeps ``y`` under ``ceiling``.

    Only samples near a peak are reduced: the needed reduction is held
    ``hold_ms`` either side of each over-ceiling sample (so the gain is already
    down when the spike arrives) and recovers along a ``release_ms`` curve in
    both directions (no step, no click). Everywhere else the gain is exactly 1.
    Fully vectorized: a limited clip can have thousands of over-ceiling
    samples (voiced peaks recur every few ms), and this runs on the event loop.
    """
    over = np.flatnonzero(np.abs(y) > ceiling)
    if len(over) == 0:
        return np.ones_like(y)
    hold = max(1, int(sample_rate * hold_ms / 1000))
    need = np.ones_like(y)
    need[over] = ceiling / np.abs(y[over])
    g = _window_min(need, hold)
    tau = max(1.0, sample_rate * release_ms / 1000)
    decay = np.exp(-1.0 / tau)  # per-sample recovery toward gain 1
    reach = int(6 * tau) + hold  # recovery is > 99.7% complete by then
    # Merge spike neighbourhoods into regions and smooth only inside them.
    starts = np.maximum(over - reach, 0)
    ends = np.minimum(over + reach + 1, len(y))
    breaks = np.flatnonzero(starts[1:] > ends[:-1]) + 1
    for a, b in zip(starts[np.r_[0, breaks]], ends[np.r_[breaks - 1, -1]]):
        depth = 1.0 - g[a:b]
        fwd = _decay_max(depth, decay)
        bwd = _decay_max(depth[::-1], decay)[::-1]
        g[a:b] = 1.0 - np.maximum(fwd, bwd)
    return g


def even_level(
    pcm: bytes,
    sample_rate: int,
    *,
    target_dbfs: float,
    max_gain_db: float = 6.0,
    peak_ceiling_dbfs: float = -1.0,
    max_limit_db: float = 6.0,
    limit_hold_ms: float = 1.5,
    limit_release_ms: float = 10.0,
) -> bytes:
    """Apply one gain so the clip's speech level lands on ``target_dbfs``.

    The gain is capped at ``±max_gain_db`` and never clips. Short v3 fillers
    ("हाँजी.", "अच्छा जी.") often come out quiet but carry one near-full-scale
    spike; rather than leave the whole clip quiet because of a few
    milliseconds, those spikes alone are pressed down (at most
    ``max_limit_db``) so the clip can reach the target. With
    ``max_limit_db=0`` the gain is simply reduced to fit under the ceiling.
    """
    level = speech_level_dbfs(pcm, sample_rate)
    if level is None:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    gain_db = float(np.clip(target_dbfs - level, -max_gain_db, max_gain_db))
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 0:
        headroom_db = peak_ceiling_dbfs - 20 * np.log10(peak / _FULL_SCALE)
        # The spike limiter can buy up to max_limit_db of extra room.
        gain_db = min(gain_db, headroom_db + max(0.0, max_limit_db))
    if abs(gain_db) < 0.05:
        return pcm
    y = x * (10 ** (gain_db / 20))
    ceiling = _FULL_SCALE * 10 ** (peak_ceiling_dbfs / 20)
    if np.max(np.abs(y)) > ceiling:
        y = y * _spike_gain(y, sample_rate, ceiling, limit_hold_ms, limit_release_ms)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def spectral_tilt_db(pcm: bytes, sample_rate: int) -> float | None:
    """Energy 1–4 kHz vs 80 Hz–1 kHz over the clip's louder frames (dB)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    win, hop = int(0.04 * sample_rate), int(0.01 * sample_rate)
    if len(x) < win * 5:
        return None
    fr = np.fft.rfftfreq(win, 1 / sample_rate)
    lo_band = (fr >= 80) & (fr < 1000)
    hi_band = (fr >= 1000) & (fr < 4000)
    ref = np.sqrt(np.mean(x**2)) + 1e-9
    window = np.hanning(win)
    lo = hi = 0.0
    n = 0
    for i in range(0, len(x) - win, hop):
        seg = x[i : i + win]
        if np.sqrt(np.mean(seg**2)) < ref * 0.5:
            continue
        spec = np.abs(np.fft.rfft(seg * window)) ** 2
        lo += spec[lo_band].sum()
        hi += spec[hi_band].sum()
        n += 1
    if n < 5:
        return None
    return float(10 * np.log10((hi + 1e-12) / (lo + 1e-12)))


def match_tilt(
    pcm: bytes, sample_rate: int, *, target_db: float, max_db: float = 4.0
) -> bytes:
    """Tilt the clip's spectrum (0 dB at 1 kHz, ±g/2 at 250 Hz / 4 kHz) so its
    band balance lands on ``target_db``; overall RMS is preserved."""
    tilt = spectral_tilt_db(pcm, sample_rate)
    if tilt is None:
        return pcm
    g = float(np.clip(target_db - tilt, -max_db, max_db))
    if abs(g) < 0.25:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n = len(x)
    nfft = 1 << (n + sample_rate // 10).bit_length()  # padded: no edge wrap
    f = np.fft.rfftfreq(nfft, 1 / sample_rate)
    octs = np.clip(np.log2(np.maximum(f, 1.0) / 1000.0) / 2.0, -1.0, 1.0)
    y = np.fft.irfft(np.fft.rfft(x, nfft) * 10 ** ((g / 2.0) * octs / 20.0), nfft)[:n]
    y *= (np.sqrt(np.mean(x**2)) + 1e-12) / (np.sqrt(np.mean(y**2)) + 1e-12)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()
