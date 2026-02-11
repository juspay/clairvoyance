"""STT-to-VAD Bridge Processor.

Sits after the STT service in the pipeline and forwards interim transcription
signals to the STTAwareSileroVADAnalyzer. This enables the dual-signal approach
where STT proving speech exists allows the VAD to bypass the volume check.

Pipeline position:
    transport.input() -> STT -> [STTVADBridge] -> response_gate -> ...
"""

from pipecat.audio.vad.vad_analyzer import VADAnalyzer
from pipecat.frames.frames import Frame, InterimTranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.agent.vad import STTAwareSileroVADAnalyzer
from app.core.logger import logger


class STTVADBridge(FrameProcessor):
    """Bridges STT interim transcriptions to the VAD analyzer.

    When an InterimTranscriptionFrame with non-empty text arrives, notifies the
    STTAwareSileroVADAnalyzer that speech has been detected by STT. This allows
    the VAD to bypass the volume threshold check for the next few seconds.

    All frames are passed through unmodified — this processor only observes.
    """

    def __init__(self, vad_analyzer: VADAnalyzer, **kwargs):
        super().__init__(**kwargs)
        self._stt_aware_analyzer: STTAwareSileroVADAnalyzer | None = None

        if isinstance(vad_analyzer, STTAwareSileroVADAnalyzer):
            self._stt_aware_analyzer = vad_analyzer
            logger.info("STTVADBridge: linked to STTAwareSileroVADAnalyzer")
        else:
            logger.info(
                "STTVADBridge: VAD analyzer is not STT-aware, bridge will be a no-op"
            )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if (
            self._stt_aware_analyzer
            and isinstance(frame, InterimTranscriptionFrame)
            and frame.text
            and frame.text.strip()
        ):
            self._stt_aware_analyzer.notify_stt_interim_transcription()

        await self.push_frame(frame, direction)
