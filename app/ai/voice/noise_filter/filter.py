"""BuddyNoiseFilter: the pipecat audio input filter that cleans caller audio.

pipecat's input transport awaits filter() for every caller frame and only
catches timeouts, so an exception here would leave the bot deaf. filter()
therefore never raises: any failure, or falling LAG_BUDGET_S behind real time,
passes the rest of the call through untouched.
"""

import asyncio
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, cast

import numpy as np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame

from app.ai.voice.noise_filter.models import LoadedModel, load_model
from app.core.logger import logger

LAG_BUDGET_S = 0.1
DEFAULT_LEVEL = 0.5
_SCALE = 32768.0


# Inference runs off the event loop (onnxruntime releases the GIL), so one
# call's frames never stall another call's audio.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="noise-filter")


class StreamEnhancer:
    """One call's STFT framing, model state and overlap-add. Not thread-safe."""

    def __init__(self, model: LoadedModel, sample_rate: int, mix: float = 1.0):
        spec = model.spec
        if sample_rate != spec.sample_rate:
            raise ValueError(
                f"{spec.name} runs at {spec.sample_rate} Hz, the call at {sample_rate} Hz"
            )
        self._model = model
        self._mix = float(mix)
        self._win, self._hop = spec.win_len, spec.hop
        self._bins = spec.win_len // 2 + 1
        self._states = [s.copy() for s in model.init_states]
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(self._win, dtype=np.float32)
        # The output lags the input by delay_hops, so the blend uses the noisy
        # frame from that many hops back (the current one would echo 40 ms apart).
        zero = np.zeros((self._bins, 2), dtype=np.float32)
        self._noisy = deque([zero] * spec.delay_hops, maxlen=spec.delay_hops + 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        self._in_buf = np.concatenate([self._in_buf, x])
        frames = []
        while self._in_buf.size >= self._win:
            frames.append(self._frame(self._in_buf[: self._win]))
            self._in_buf = self._in_buf[self._hop :]
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

    def _frame(self, chunk: np.ndarray) -> np.ndarray:
        model = self._model
        spec = np.fft.rfft(chunk * model.window)
        ri = np.empty((self._bins, 2), dtype=np.float32)
        ri[:, 0], ri[:, 1] = spec.real, spec.imag
        feed = {model.spec_input: ri.reshape(model.spec_shape)}
        feed.update(zip(model.state_inputs, self._states))
        out = cast(list[np.ndarray], model.session.run(None, feed))
        enh = out[0].reshape(self._bins, 2)
        if not np.isfinite(enh).all():
            raise FloatingPointError(f"{model.spec.name} produced non-finite output")
        self._states = out[1:]
        self._noisy.append(ri)
        if self._mix < 1.0:
            enh = self._mix * enh + (1.0 - self._mix) * self._noisy[0]
        frame = np.fft.irfft(enh[:, 0] + 1j * enh[:, 1], n=self._win) * model.window
        self._out_buf += frame.astype(np.float32)
        done = self._out_buf[: self._hop].copy()
        self._out_buf[: -self._hop] = self._out_buf[self._hop :]
        self._out_buf[-self._hop :] = 0.0
        return done


class BuddyNoiseFilter(BaseAudioFilter):
    """Real-time noise suppression for one call's incoming audio."""

    def __init__(
        self,
        *,
        model: str,
        model_path: Path,
        enhancement_level: Optional[float] = None,
    ) -> None:
        if enhancement_level is not None and not 0.0 <= enhancement_level <= 1.0:
            raise ValueError("'enhancement_level' must be between 0.0 and 1.0.")
        self._model_name = model
        self._model_path = str(model_path)
        self._mix = DEFAULT_LEVEL if enhancement_level is None else enhancement_level
        self._enhancer: Optional[StreamEnhancer] = None
        self._lock = threading.Lock()  # one process() at a time, even after a cancel
        self._lag = 0.0
        self._rate = 0
        self._enabled = True

    async def start(self, sample_rate: int):
        if self._mix == 0.0:
            return  # level 0 is untouched audio; skip the model
        try:
            model = await asyncio.to_thread(
                load_model, self._model_name, self._model_path
            )
            self._rate = sample_rate
            self._enhancer = StreamEnhancer(model, sample_rate, mix=self._mix)
        except Exception as e:
            self._enhancer = None
            logger.warning(f"Noise filter unavailable, proceeding without it: {e}")

    async def stop(self):
        self._enhancer = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._enabled = frame.enable

    def _process(
        self, enhancer: StreamEnhancer, x: np.ndarray
    ) -> tuple[np.ndarray, float]:
        """Enhance on the worker; also return when the work finished."""
        with self._lock:
            y = enhancer.process(x)
        return y, time.perf_counter()

    async def filter(self, audio: bytes) -> bytes:
        enhancer = self._enhancer
        if enhancer is None or not self._enabled or not audio or len(audio) % 2:
            return audio
        started = time.perf_counter()
        try:
            x = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / _SCALE
            y, finished = await asyncio.get_running_loop().run_in_executor(
                _EXECUTOR, self._process, enhancer, x
            )
            out = np.clip(y * _SCALE, -_SCALE, _SCALE - 1).astype(np.int16).tobytes()
        except Exception as e:
            self._enhancer = None
            logger.error(f"Noise filter failed mid-call, passing audio through: {e}")
            return audio
        # Backlog: queue + compute time beyond the audio's own duration, never below
        # zero. Timed on the worker: an event-loop stall is not the filter falling
        # behind, and passthrough would not win that time back.
        took = finished - started
        self._lag = max(0.0, self._lag + took - x.size / self._rate)
        if self._lag > LAG_BUDGET_S:
            self._enhancer = None
            logger.warning(
                f"Noise filter fell {self._lag * 1e3:.0f} ms behind real time; "
                "passing audio through for the rest of the call"
            )
        return out
