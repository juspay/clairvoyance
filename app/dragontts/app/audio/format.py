"""Audio format conversion — native format -> requested output_format.

The cache stores audio in the provider's *native* format and converts to the
caller's requested ``output_format`` on serve (the key is format-agnostic, so
one entry serves every format). Handles native input of either raw PCM
(pcm_s16le) or μ-law (ulaw/mulaw, G.711) — the formats clairvoyance's telephony
path uses. An MP3 decode path can be added later without touching the cache
layer.
"""

from __future__ import annotations

import audioop

_ULAWS = {"ulaw", "mulaw", "pcm_mulaw"}
_PCMS = {"pcm_s16le", "pcm", "raw"}


def _downsample_pcm(data: bytes, in_rate: int, out_rate: int, sample_width: int = 2) -> bytes:
    if in_rate == out_rate:
        return data
    # Pad to a whole number of frames before rate conversion.
    if len(data) % sample_width != 0:
        data = data + b"\x00" * (sample_width - (len(data) % sample_width))
    out, _ = audioop.ratecv(data, sample_width, 1, in_rate, out_rate, None)
    return out


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
    pcm = _downsample_pcm(pcm, native_rate, out_rate)

    # 3. Encode to the target format.
    if out_enc in _ULAWS:
        return audioop.lin2ulaw(pcm, 2)
    if out_enc in _PCMS:
        return pcm
    raise ValueError(f"Unsupported output encoding: {out_encoding!r}")


def content_type_for(encoding: str) -> str:
    """HTTP content type for a cached audio encoding."""
    enc = encoding.lower()
    if enc in _ULAWS:
        return "audio/mulaw"
    if enc in _PCMS:
        return "audio/pcm"
    return "application/octet-stream"
