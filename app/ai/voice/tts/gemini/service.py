"""Low-latency Gemini TTS service.

Extends pipecat's ``GeminiTTSService`` to override the hardcoded ~500 ms
first-frame audio buffer (``CHUNK_SECONDS = 0.5`` in ``TTSService``).
Pipecat's default downloads ~500 ms of audio before emitting the first
``TTSAudioRawFrame`` downstream — designed to prevent glitches when the TTS
provider streams slowly. For Gemini TTS (which streams at a steady rate)
this adds ~500 ms of hidden mouth-to-ear latency that the standard TTFB
metric does not capture.

This subclass reduces the first-frame buffer to ~100 ms, trading a tiny
risk of initial-audio underrun (absorbed by the downstream telephony
jitter buffer) for ~400 ms lower perceived latency on every turn.

This is a thin override — once pipecat exposes ``CHUNK_SECONDS`` as a
configurable parameter this module can be removed.
"""

from __future__ import annotations

from pipecat.services.google.tts import GeminiTTSService

__all__ = ["LowLatencyGeminiTTSService"]

# Pipecat default is 0.5 s. We override to 0.1 s.
# At 24 kHz × 2 bytes/sample, this is 4800 bytes per chunk (~100 ms of audio).
_LOW_LATENCY_CHUNK_SECONDS = 0.1


class LowLatencyGeminiTTSService(GeminiTTSService):
    """GeminiTTSService variant with a reduced first-frame audio buffer.

    Overrides ``TTSService.chunk_size`` to use a 100 ms buffer instead of
    pipecat's default 500 ms. Saves ~400 ms of mouth-to-ear latency per
    turn on Gemini TTS.

    Selected via the ``BB_GEMINI_TTS_LOW_LATENCY`` Redis flag in
    ``build_gemini_tts``. If set to False, falls back to the standard
    ``GeminiTTSService``.

    .. note::
        Do NOT use ``text_aggregation_mode=TOKEN`` (or
        ``aggregate_sentences=False``) with Gemini TTS. Pipecat opens a
        fresh ``streaming_synthesize`` gRPC stream per ``run_tts()`` call,
        so token-mode would mean one new stream + ~1 s first-audio
        latency per LLM token. Keep ``aggregate_sentences=True`` and
        reduce perceived latency by making the LLM start each response
        with a short period-terminated acknowledgement (``Okay.``,
        ``സർ.``, etc.) so the first sentence flushes to TTS after a few
        tokens instead of ~25.
    """

    @property
    def chunk_size(self) -> int:
        return int(self.sample_rate * _LOW_LATENCY_CHUNK_SECONDS * 2)
