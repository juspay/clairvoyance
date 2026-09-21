"""Audio format conversion — native format -> requested output_format.

The cache stores audio in the provider's *native* format and converts to the
caller's requested ``output_format`` on serve (the key is format-agnostic, so
one entry serves every format). Handles native input of either raw PCM
(pcm_s16le) or μ-law (ulaw/mulaw, G.711) — the formats clairvoyance's telephony
path uses. An MP3 decode path can be added later without touching the cache
layer.

Rate conversion is a Hann-windowed-sinc resampler (anti-aliased): anything
above the output band is low-passed away instead of folding back into it. This
matters twice on the telephony path — ElevenLabs v3 synthesizes (and atempo
time-stretches) at its full native rate, so stretch artifacts land above 4 kHz
and are filtered here rather than baked into the voice band, and speech
sibilance above the 8 kHz output band no longer aliases down into it the way
the previous unfiltered ``audioop.ratecv`` interpolation let it.
"""

from __future__ import annotations

import audioop

import numpy as np

_ULAWS = {"ulaw", "mulaw", "pcm_mulaw"}
_PCMS = {"pcm_s16le", "pcm", "raw"}

# Kernel half-width measured in cycles of the cutoff frequency. 6 cycles
# puts the stopband at the Hann window's first sidelobe (~-31 dB) — at or
# below the mu-law noise floor, so more would buy nothing audible while
# costing serve-time CPU on every cache hit.
_HALF_CYCLES = 6
_MIN_HALF = 16  # taps for the upsample case (cutoff 0.45 → ~13; keep 16)
# Cap on kernel elements per vectorized block, so long clips don't allocate
# an (outputs × taps) matrix in one shot.
_ELEMS_PER_BLOCK = 1 << 22


def _resample_pcm(data: bytes, in_rate: int, out_rate: int) -> bytes:
    if in_rate == out_rate:
        return data
    # Pad to a whole number of frames before rate conversion.
    if len(data) % 2 != 0:
        data = data + b"\x00"
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    step = in_rate / out_rate
    # Cutoff at 0.45 cycles/sample of the LOWER rate: downsampling low-passes
    # everything above the output band (anti-aliasing); upsampling simply
    # reconstructs the input band.
    fc = 0.45 * min(1.0, out_rate / in_rate)
    half = max(_MIN_HALF, int(np.ceil(_HALF_CYCLES / fc)))
    offs = np.arange(-half + 1, half + 1, dtype=np.int64)
    n_out = int(np.ceil(len(x) * out_rate / in_rate))
    block = max(1, _ELEMS_PER_BLOCK // (2 * half))
    parts: list[bytes] = []
    for start in range(0, n_out, block):
        j = np.arange(start, min(start + block, n_out), dtype=np.float32)
        pos = j * step
        base = np.floor(pos).astype(np.int64)[:, None] + offs[None, :]
        np.clip(base, 0, len(x) - 1, out=base)
        d = base.astype(np.float32) - pos[:, None]
        # 2*fc: numpy's sinc(x)=sin(pi x)/(pi x), and the ideal lowpass at
        # cutoff fc cycles/sample is sinc(2*fc*d). Dropping the 2 rolled the
        # real cutoff down to fc/2 (~1.8 kHz for 44.1k->8k), muffling the
        # voice's entire 2-4 kHz presence band.
        w = np.sinc(2.0 * fc * d) * (0.5 + 0.5 * np.cos(np.pi * d / half))
        w /= w.sum(axis=1, keepdims=True)  # unity DC gain per output sample
        y = (x[base] * w).sum(axis=1)
        parts.append(np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes())
    return b"".join(parts)


def convert_audio(
    native_audio: bytes,
    *,
    native_encoding: str,
    native_rate: int,
    out_encoding: str,
    out_rate: int,
) -> bytes:
    """Convert native-format audio to the requested output format.

    Native input may be PCM s16le or μ-law. The pipeline normalizes to PCM,
    resamples to the target rate, then encodes to the target format.
    """
    native_enc = native_encoding.lower()
    out_enc = out_encoding.lower()

    # 1. Normalize native -> PCM s16le at the native sample rate.
    if native_enc in _ULAWS:
        pcm = audioop.ulaw2lin(native_audio, 2)
    else:  # already PCM s16le
        pcm = native_audio

    # 2. Resample PCM to the target rate.
    pcm = _resample_pcm(pcm, native_rate, out_rate)

    # 3. Encode to the target format.
    if out_enc in _ULAWS:
        return audioop.lin2ulaw(pcm, 2)
    if out_enc in _PCMS:
        return pcm
    raise ValueError(f"Unsupported output encoding: {out_encoding!r}")


def apply_presence_boost(pcm: bytes, sample_rate: int, boost_db: float) -> bytes:
    """Zero-phase presence bell — partial "spark" recovery for telephony.

    Band-limiting to 8 kHz deletes everything above ~3.4 kHz (sibilance,
    breath "air", consonant crispness — the "spark"), and that loss is
    irreducible at the telephony rate. What this CAN do is re-weight the top
    octave that survives (300-3400 Hz): a raised-cosine bell peaking at
    2.8 kHz, unity below 2.0 kHz and above 3.6 kHz, pushes the crispness
    region forward so the band-limited voice reads brighter — the classic
    telephony voice-enhancement trick. FFT-based, so it is zero-phase and
    runs once per synthesis (never on a cache hit).

    ``boost_db`` may be negative (a gentle cut). Returns the input unchanged
    for a no-op boost, too-short input, or rates where the band is unreachable.
    """
    if abs(boost_db) < 0.05 or not pcm or sample_rate < 8000:
        return pcm
    if len(pcm) % 2 != 0:
        pcm = pcm + b"\x00"
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sample_rate)
    gain = np.ones_like(freqs)
    rise = (freqs >= 2000.0) & (freqs < 2800.0)
    fall = (freqs >= 2800.0) & (freqs <= 3600.0)
    # Half-cosine skirts: unity at 2.0 kHz, full boost at 2.8 kHz, back to
    # unity at 3.6 kHz — smooth enough to stay free of ringing.
    bell = 10.0 ** (boost_db / 20.0)
    gain[rise] = 1.0 + (bell - 1.0) * (
        0.5 - 0.5 * np.cos(np.pi * (freqs[rise] - 2000.0) / 800.0)
    )
    gain[fall] = 1.0 + (bell - 1.0) * (
        0.5 + 0.5 * np.cos(np.pi * (freqs[fall] - 2800.0) / 800.0)
    )
    y = np.fft.irfft(spec * gain, n=len(x))
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


def content_type_for(encoding: str) -> str:
    """HTTP content type for a cached audio encoding."""
    enc = encoding.lower()
    if enc in _ULAWS:
        return "audio/mulaw"
    if enc in _PCMS:
        return "audio/pcm"
    return "application/octet-stream"
