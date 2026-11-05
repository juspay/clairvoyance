"""Utterance hygiene — trim dead air and gate the noise floor.

ElevenLabs v3 generations carry (measured on production 44.1 kHz clips):

- ~200-500 ms of TRAILING silence per clip (~300 ms average) and ~60-140 ms
  leading — dead air that reads as lag and drowns the atempo speed-up;
- occasional internal pauses of 260-440 ms from generation randomness;
- a stationary noise floor (breath/room-tone) only ~22-27 dB under the
  speech — audible as background hiss during every pause.

``clean_utterance`` trims the pads to small caps, caps internal pauses, and
attenuates the remaining retained silence toward digital silence (with
one-frame ramps at boundaries so they don't click).

DATA-LOSS DIAL: ``content_factor`` sets the line between "actual TTS data"
(never deleted, never capped, never gated — only passed through) and
"silence/noise" (trimmable/cappable/gatable). The default 0.15 (≈ −16.5 dB
under the clip's p95, equal to the speech-loud line) is the CALL-APPROVED
sound — byte-identical to the chain the approved comparisons and recordings
were made with. A LOWER factor keeps more: 0.09 (≈ −21 dB) is the maximally
protective setting — under it every measured v3 noise floor (−22..−27 dB)
still cleans, but soft tails and quiet pauses down to −21 dB survive too
(~+100 ms per clip on real takes — audibly softer edges). Both ends are
safe for speech: word material never approaches either line.
"""

from __future__ import annotations

import numpy as np

_FRAME_MS = 20
_START_LOUD_FRAMES = 2  # consecutive loud frames needed to enter speech
_STOP_QUIET_FRAMES = 3  # consecutive quiet frames needed to leave speech


def _classify(rms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hysteresis speech mask + the raw loud/quiet frame classification.

    ``loud`` drives the speech/gate hysteresis, ``speech`` drives the gate:
    its 2-3 frame hangover keeps fricative onsets/tails from being
    attenuated, at the cost of ~40 ms of un-gated noise at pause edges —
    inaudible at the measured noise-floor level.
    """
    thr = max(float(np.percentile(rms, 95)) * 0.15, 200.0)
    loud = rms > thr
    m = len(rms)
    speech = np.zeros(m, dtype=bool)
    in_speech = False
    quiet_run = loud_run = 0
    for i in range(m):
        if in_speech:
            speech[i] = True
            if loud[i]:
                quiet_run = 0
            else:
                quiet_run += 1
                if quiet_run >= _STOP_QUIET_FRAMES:
                    in_speech = False
        elif loud[i]:
            loud_run += 1
            if loud_run >= _START_LOUD_FRAMES:
                in_speech = True
                speech[i] = True
                speech[i - 1] = True  # include the run's first frame
        else:
            loud_run = 0
    return speech, loud


def clean_utterance(
    pcm: bytes,
    sample_rate: int,
    *,
    lead_ms: int = 60,
    tail_ms: int = 120,
    max_pause_ms: int = 300,
    gate_floor: float = 0.15,
    content_factor: float = 0.15,
    content_abs: float = 200.0,
) -> bytes:
    """Trim silence pads, cap internal pauses, attenuate retained silence.

    Returns s16le mono PCM. Frames above the content line
    (``max(p95 * content_factor, content_abs)``) are CONTENT and reach the
    output untouched; sub-line frames are silence/noise: pads are trimmed to
    ``lead_ms`` / ``tail_ms`` caps, pauses longer than ``max_pause_ms`` are
    shortened, and the retained remainder is scaled to ``gate_floor``
    (0.15 ≈ -16 dB — much quieter hiss, but room-tone continuity survives;
    0.0 = dead digital silence). Boundary frames ramp between the speech
    level and the floor so transitions stay smooth.

    ``content_factor`` defaults to 0.15 (the loud line) = the call-approved
    chain; lower it (e.g. 0.09) to keep progressively more quiet material —
    see the module docstring's DATA-LOSS DIAL.
    """
    frame = max(1, sample_rate * _FRAME_MS // 1000)
    if len(pcm) < 3 * frame * 2:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2")
    m = len(x) // frame
    frames = x[: m * frame].reshape(m, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1))

    speech, _loud = _classify(rms)
    p95 = float(np.percentile(rms, 95))
    content = rms > max(p95 * content_factor, content_abs)
    if not content.any():
        return pcm
    first = int(np.argmax(content))
    last = m - 1 - int(np.argmax(content[::-1]))

    keep = np.zeros(m, dtype=bool)
    # Keep content plus capped pads on each side.
    keep[
        max(first - lead_ms // _FRAME_MS, 0) : min(last + 1 + tail_ms // _FRAME_MS, m)
    ] = True
    pause_frames = max_pause_ms // _FRAME_MS
    run = 0
    for i in range(first, last + 1):
        if content[i]:
            run = 0
        else:
            run += 1
            if run > pause_frames:
                keep[i] = False
    if not keep.any():
        return pcm

    ramp_down = gate_floor + (1.0 - gate_floor) * np.linspace(1.0, 0.0, frame)
    ramp_up = gate_floor + (1.0 - gate_floor) * np.linspace(0.0, 1.0, frame)
    gated = frames.copy()
    for i in range(m):
        if keep[i] and not speech[i] and not content[i]:
            # Only frames under the content line are attenuated — content
            # passes through at its own level.
            if i > 0 and speech[i - 1]:
                gated[i] = frames[i] * ramp_down
            elif i + 1 < m and speech[i + 1]:
                gated[i] = frames[i] * ramp_up
            else:
                gated[i] = frames[i] * gate_floor

    return gated[keep].astype("<i2").tobytes()
