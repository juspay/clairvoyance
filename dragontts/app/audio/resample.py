"""Stateful streaming PCM resampler (vectorized numpy).

Providers whose streaming APIs emit PCM at a rate other than the native cache
rate (16 kHz) need to resample *incrementally* to forward live 16 kHz chunks on
a cache miss (low TTFB) instead of buffering the whole clip:

- Gemini streams raw PCM at 24 kHz over gRPC.
- Sarvam ``bulbul:v2`` / ``bulbul:v3`` stream at 22050 / 24000 Hz over WS.

This module feeds each upstream chunk through a linear-interpolation resampler
that carries the fractional read position and a small tail across calls, so
seams align chunk-to-chunk. Pure numpy (no scipy); same linear-interp quality
as the one-shot Gemini synth path. For speech the sub-sample position drift over
very long clips is inaudible.

When ``out_rate < in_rate`` (downsampling) high-frequency input content above
the output Nyquist can alias slightly, exactly as the one-shot linear path
does; acceptable for speech and matches existing behaviour.
"""

from __future__ import annotations

import numpy as np


class StreamingResampler:
    """int16 LE PCM at ``in_rate`` -> int16 LE PCM at ``out_rate``, streamed.

    Feed chunks via :meth:`push` and finish with :meth:`flush` to drain the tail.
    Holds a small leftover buffer + a fractional read position between calls so
    chunk boundaries don't introduce a discontinuity.
    """

    def __init__(self, in_rate: int, out_rate: int):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("rates must be positive")
        self._in_rate = in_rate
        self._out_rate = out_rate
        # Input samples advanced per emitted output sample.
        self._step = in_rate / out_rate
        self._carry = np.empty(0, dtype=np.float32)  # leftover input (float)
        # Fractional input index (relative to the start of ``_carry``) of the
        # next output sample to emit.
        self._pos = 0.0
        # A single held byte from an odd-length chunk (trailing half-frame);
        # prepended to the next push so we only ever feed whole int16 frames.
        self._held_byte: bytes = b""

    def push(self, data: bytes) -> bytes:
        """Resample one chunk of int16 LE PCM; return resampled int16 LE bytes.

        May return ``b""`` when there isn't enough buffered input to emit a
        whole output sample yet (the bytes are held for the next push).
        """
        if not data:
            return b""
        data = self._held_byte + data
        extra = len(data) & 1
        if extra:  # odd length: keep the trailing byte for the next call
            self._held_byte = data[-1:]
            data = data[:-1]
        else:
            self._held_byte = b""
        if not data:
            return b""
        x = np.frombuffer(data, dtype="<i2").astype(np.float32)
        buf = np.concatenate([self._carry, x]) if self._carry.size else x
        return self._resample(buf)

    def _resample(self, buf: np.ndarray) -> bytes:
        n = buf.shape[0]
        # An output sample at fractional input index ``idx`` uses samples
        # ``floor(idx)`` and ``floor(idx)+1``; both must be real samples, i.e.
        # ``floor(idx) <= n-2``. Compute a generous candidate count, then drop
        # any candidate whose ``lo`` has no following sample (the off-by-one at
        # the tail that would otherwise invent a phantom sample past the buffer).
        avail = n - 1 - self._pos
        if avail <= 0:
            # Not enough room for even one output — keep it all, wait for more.
            self._carry = buf
            return b""
        k = int(avail // self._step) + 1  # generous upper bound (>=1)
        ks = np.arange(k, dtype=np.float64)
        indices = self._pos + ks * self._step
        lo = np.floor(indices).astype(np.int64)
        mask = lo <= n - 2  # both interpolation samples must exist
        if not mask.any():
            self._carry = buf
            return b""
        # ``lo`` is non-decreasing so the True entries are a leading prefix,
        # but boolean-index to be robust regardless of ordering.
        indices = indices[mask]
        lo = lo[mask]
        hi = lo + 1  # now guaranteed <= n-1 by the mask
        frac = (indices - lo).astype(np.float32)
        vals = buf[lo] + frac * (buf[hi] - buf[lo])
        out = np.clip(vals, -32768.0, 32767.0).astype("<i2").tobytes()
        # Carry from the last input index actually used so the next call can
        # interpolate across the seam; advance the fractional position relative
        # to the trimmed carry.
        last_lo = int(lo[-1])
        self._carry = buf[last_lo:].copy()
        self._pos += len(lo) * self._step - last_lo
        return out

    def flush(self) -> bytes:
        """Drain any trailing output by extending the carry with its last sample.

        Emits at most one or two samples, so the cached clip is at most a
        fraction of a millisecond short — negligible.
        """
        if self._carry.size == 0:
            return b""
        buf = np.concatenate([self._carry, self._carry[-1:]])
        out = self._resample(buf)
        self._carry = np.empty(0, dtype=np.float32)
        self._pos = 0.0
        return out

    @property
    def in_rate(self) -> int:
        return self._in_rate

    @property
    def out_rate(self) -> int:
        return self._out_rate


def resample_clip(data: bytes, in_rate: int, out_rate: int) -> bytes:
    """One-shot convenience: resample a complete int16 LE PCM clip.

    Equivalent to feeding the whole buffer through a fresh
    :class:`StreamingResampler` and flushing. Kept as a named helper so callers
    that already have the full clip (the one-shot synth path) read clearly.
    """
    rs = StreamingResampler(in_rate, out_rate)
    return rs.push(data) + rs.flush()
