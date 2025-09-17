import os

import numpy as np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame

from app.core.config import ENABLE_KRISP_FILTER, KRISP_MODEL_PATH
from app.core.logger import logger

# Optional import for krisp_audio
try:
    import krisp_audio

    KRISP_AVAILABLE = True
except ImportError:
    KRISP_AVAILABLE = False
    krisp_audio = None


def log_callback(log_message, log_level):
    logger.info(f"[{log_level}] {log_message}")


class NoiseFilterFromKrisp(BaseAudioFilter):
    # Initialize krisp-specific constants only if available and enabled
    if KRISP_AVAILABLE and ENABLE_KRISP_FILTER:
        krisp_audio.globalInit("", log_callback, krisp_audio.LogLevel.Off)
        SDK_VERSION = krisp_audio.getVersion()
        logger.info(
            f"Krisp Audio Python SDK Version: {SDK_VERSION.major}."
            f"{SDK_VERSION.minor}.{SDK_VERSION.patch}"
        )
        SAMPLE_RATES = {
            8000: krisp_audio.SamplingRate.Sr8000Hz,
            16000: krisp_audio.SamplingRate.Sr16000Hz,
            24000: krisp_audio.SamplingRate.Sr24000Hz,
            32000: krisp_audio.SamplingRate.Sr32000Hz,
            44100: krisp_audio.SamplingRate.Sr44100Hz,
            48000: krisp_audio.SamplingRate.Sr48000Hz,
        }
    else:
        # Fallback values when krisp is not available
        SAMPLE_RATES = {
            8000: 8000,
            16000: 16000,
            24000: 24000,
            32000: 32000,
            44100: 44100,
            48000: 48000,
        }
        if ENABLE_KRISP_FILTER and not KRISP_AVAILABLE:
            logger.warning(
                "Krisp filter is enabled but krisp_audio module is not available. Audio will pass through unfiltered."
            )

    def __init__(self, model_path: str = None):
        super().__init__()

        # Only validate model path if krisp is available and enabled
        if KRISP_AVAILABLE and ENABLE_KRISP_FILTER:
            model_path = model_path or KRISP_MODEL_PATH
            if not model_path:
                raise Exception("Model path is not set")
            if not model_path.endswith(".kef"):
                raise Exception("Model is expected with .kef extension")
            if not os.path.isfile(model_path):
                raise Exception(f"Model file not found: {model_path}")
        elif not model_path:
            model_path = KRISP_MODEL_PATH or ""

        self._model_path = model_path
        self._filtering = ENABLE_KRISP_FILTER
        self._session = None
        self._samples_per_frame = None
        self._noise_suppression_level = 100
        self._krisp_functional = KRISP_AVAILABLE and ENABLE_KRISP_FILTER

    def _int_to_sample_rate(self, sample_rate):
        if sample_rate not in self.SAMPLE_RATES:
            raise ValueError("Unsupported sample rate")
        return self.SAMPLE_RATES[sample_rate]

    async def start(self, sample_rate: int):
        if self._krisp_functional:
            model_info = krisp_audio.ModelInfo()
            model_info.path = self._model_path
            nc_cfg = krisp_audio.NcSessionConfig()
            nc_cfg.inputSampleRate = self._int_to_sample_rate(sample_rate)
            nc_cfg.inputFrameDuration = krisp_audio.FrameDuration.Fd10ms
            nc_cfg.outputSampleRate = nc_cfg.inputSampleRate
            nc_cfg.modelInfo = model_info
            self._samples_per_frame = int((sample_rate * 10) / 1000)
            self._session = krisp_audio.NcInt16.create(nc_cfg)
        else:
            # Fallback mode - just set up frame size for pass-through
            self._samples_per_frame = int((sample_rate * 10) / 1000)
            self._session = None

    async def stop(self):
        self._session = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._filtering = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        # Return unfiltered audio if filtering is disabled or krisp is not functional
        if not self._filtering or not self._krisp_functional or not self._session:
            return audio

        # Krisp filtering logic
        samples = np.frombuffer(audio, dtype=np.int16)
        if samples.size % self._samples_per_frame:
            raise ValueError(
                f"Audio length {samples.size} samples is not a multiple of {self._samples_per_frame}"
            )
        frames = samples.reshape(-1, self._samples_per_frame)
        processed_samples = np.empty_like(samples)
        for i, frame in enumerate(frames):
            cleaned_frame = self._session.process(frame, self._noise_suppression_level)
            processed_samples[
                i * self._samples_per_frame : (i + 1) * self._samples_per_frame
            ] = cleaned_frame
        return processed_samples.tobytes()
