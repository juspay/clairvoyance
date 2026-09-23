"""End release — let a v3 sentence's final syllable go instead of stopping dead.

ElevenLabs v3 ends every clip with a ~13 dB drop inside 10 ms, heard as the
voice being cut off at each full stop. :func:`soften_end` runs on the
``_clean_tempo`` chain, once per clip, before the clip is cached: a 90 ms
raised-cosine release on the final syllable, then the air after it faded to
zero. Nothing else in the clip is touched — no pitch, timing or level edits.
"""

from __future__ import annotations

import numpy as np


def soften_end(
    pcm: bytes,
    sample_rate: int,
    *,
    release_ms: float = 90.0,
    depth_db: float = 14.0,
    content_factor: float = 0.15,
    content_abs: float = 200.0,
) -> bytes:
    """Let the final syllable go instead of stopping dead.

    v3 clips end with a ~13 dB drop inside 10 ms. This fades the last
    ``release_ms`` of the VOICE (up to its last loud 5 ms frame, found with the
    same content line hygiene uses) down to ``-depth_db`` along a
    raised-cosine curve, then fades the air left after it (hygiene's gated
    tail) on down to zero, so the clip never ends by stepping from room tone
    into dead digital silence (a ~78 dB step in 40/44 measured clips).
    Measured at 90 ms: abrupt endings 18% -> 2%, loud-to-silent 40 -> 60 ms.
    """
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    hop = max(1, sample_rate // 200)
    m = len(x) // hop
    if m < 10 or release_ms <= 0:
        return pcm
    rms = np.sqrt(np.mean(x[: m * hop].reshape(m, hop) ** 2, axis=1))
    content = rms > max(float(np.percentile(rms, 95)) * content_factor, content_abs)
    if not content.any():
        return pcm
    end = (m - int(np.argmax(content[::-1]))) * hop  # just past the last loud frame
    n = min(int(sample_rate * release_ms / 1000), end)
    floor = 10 ** (-depth_db / 20)
    g = np.ones(len(x))
    if n > 0:
        g[end - n : end] = floor + (1 - floor) * (
            0.5 + 0.5 * np.cos(np.linspace(0, np.pi, n))
        )
    rest = len(x) - end
    if rest > 0:
        g[end:] = floor * (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, rest)))
    return np.clip(np.round(x * g), -32768, 32767).astype("<i2").tobytes()
