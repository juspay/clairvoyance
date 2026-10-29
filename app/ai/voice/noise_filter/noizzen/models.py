"""Noise-suppression models: the registry and the once-per-process engine loader.

Each model runs on the Noizzen engine (engine/), built from its ONNX file at image build:
<stem>.weights is memory-mapped once per process and shared by every call; each call
opens a stream holding its own recurrent state. Sources and licence: docs/NOISE_FILTER.md.
"""

import ctypes
import platform
import threading
import weakref
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from app.core.logger import logger

ENGINE_ABI = 2
_HEADER = 64


@dataclass(frozen=True)
class ModelSpec:
    name: str
    stem: str  # <stem>.weights + <stem>.<variant>.so in BREEZE_BUDDY_NOISE_MODEL_DIR
    sample_rate: int
    win_len: int
    hop: int
    delay_hops: int  # look-ahead: the output frame lags the input by this many hops

    @property
    def file(self) -> str:
        return f"{self.stem}.weights"


# Ceva DPDFNet; both look 4 hops (40 ms) ahead.
MODELS = {
    "dpdfnet2_8k": ModelSpec("dpdfnet2_8k", "dpdfnet2_8khz", 8000, 160, 80, 4),
    "dpdfnet_base_16k": ModelSpec(
        "dpdfnet_base_16k", "dpdfnet_baseline_16khz", 16000, 320, 160, 4
    ),
}

_X86_64_V3 = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "movbe", "xsave", "abm"}


def _cpu_flags() -> set:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("flags"):
                    return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


@lru_cache(maxsize=None)
def cpu_variants() -> tuple:
    """Engine builds this CPU can run, best first (build.py compiles each of them)."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return (("x86-64-v3",) if _X86_64_V3 <= _cpu_flags() else ()) + ("x86-64-v2",)
    if machine in ("arm64", "aarch64"):
        return ("arm64",)
    return ()


def engine_library(spec: ModelSpec, model_dir: Path) -> Optional[Path]:
    for variant in cpu_variants():
        path = model_dir / f"{spec.stem}.{variant}.so"
        if path.is_file():
            return path
    return None


def find_model_file(name: str, model_dir: Path) -> Optional[Path]:
    """The model's weights file, if it and an engine build for this CPU are provisioned."""
    spec = MODELS[name]
    path = model_dir / spec.file
    if path.is_file() and engine_library(spec, model_dir) is not None:
        return path
    return None


def vorbis_window(n: int) -> np.ndarray:
    """DPDFNet's analysis/synthesis window (power-complementary at 50% overlap)."""
    s = np.sin(0.5 * np.pi * (np.arange(n) + 0.5) / (n / 2))
    return np.sin(0.5 * np.pi * s * s).astype(np.float32)


_F32 = np.ctypeslib.ndpointer(dtype=np.float32, flags="C_CONTIGUOUS")


def _declare(lib: ctypes.CDLL) -> None:
    lib.noizzen_abi_version.restype = ctypes.c_int
    lib.noizzen_spec_size.restype = ctypes.c_int
    lib.noizzen_state_size.restype = ctypes.c_int
    lib.noizzen_dbg_size.restype = ctypes.c_long
    lib.noizzen_model_open.restype = ctypes.c_void_p
    lib.noizzen_model_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.noizzen_stream_open.restype = ctypes.c_void_p
    lib.noizzen_stream_open.argtypes = [ctypes.c_void_p]
    lib.noizzen_stream_close.argtypes = [ctypes.c_void_p]
    lib.noizzen_stream_reset.argtypes = [ctypes.c_void_p]
    lib.noizzen_stream_step.argtypes = [ctypes.c_void_p, _F32, _F32]
    lib.noizzen_stream_step_state.argtypes = [
        ctypes.c_void_p,
        _F32,
        _F32,
        _F32,
        _F32,
        ctypes.c_void_p,
    ]


@dataclass(frozen=True)
class LoadedModel:
    """A model's engine and memory-mapped weights, shared by every call in the process."""

    spec: ModelSpec
    window: np.ndarray
    lib: ctypes.CDLL
    handle: int  # the engine's model: weights mapped read-only, never freed

    def open_stream(self) -> "EngineStream":
        return EngineStream(self)


class EngineStream:
    """One call's recurrent state and scratch inside the engine. Not thread-safe."""

    def __init__(self, model: LoadedModel):
        handle = model.lib.noizzen_stream_open(model.handle)
        if not handle:
            raise MemoryError("noise filter engine: could not allocate a stream")
        self._lib, self._handle = model.lib, handle
        bins = model.spec.win_len // 2 + 1
        self._shape = (bins, 2)
        # Frees the engine's buffers once nothing references this stream, including when a call
        # drops its filter mid-frame: the worker still holding it keeps it alive until it returns.
        self._close = weakref.finalize(self, model.lib.noizzen_stream_close, handle)

    def step(self, ri: np.ndarray) -> np.ndarray:
        """One frame: [bins, 2] real/imag spectrum in, the model's enhanced spectrum out."""
        if ri.shape != self._shape or ri.dtype != np.float32:
            raise ValueError(
                f"engine expects a {self._shape} float32 frame, got {ri.shape} {ri.dtype}"
            )
        out = np.empty(self._shape, np.float32)
        self._lib.noizzen_stream_step(self._handle, np.ascontiguousarray(ri), out)
        return out

    def close(self) -> None:
        self._close()


_failed: dict = {}
_load_lock = threading.Lock()  # calls starting together load the model once


def load_model(name: str, path: str) -> LoadedModel:
    """Load and validate a model once per process; a failed load is not retried."""
    key = (name, str(path))
    with _load_lock:
        if key in _failed:
            # Fresh traceback: re-raising the cached one would keep every call's frames.
            raise _failed[key].with_traceback(None)
        try:
            return _load(name, str(path))
        except Exception as e:
            _failed[key] = e
            raise


def _check_header(spec: ModelSpec, path: str) -> None:
    with open(path, "rb") as f:
        head = f.read(_HEADER)
    if len(head) != _HEADER or head[:8] != b"NOIZZEN1":
        raise ValueError(f"{spec.name}: not an engine weights file: {path}")
    version, _, _, _, _, rate, win, hop = np.frombuffer(head[8:40], "<u4")
    if version != 1 or (rate, win, hop) != (spec.sample_rate, spec.win_len, spec.hop):
        raise ValueError(
            f"{spec.name}: weights are for {rate} Hz / {win} / {hop}, "
            f"spec says {spec.sample_rate} Hz / {spec.win_len} / {spec.hop}"
        )


@lru_cache(maxsize=None)
def _load(name: str, path: str) -> LoadedModel:
    spec = MODELS[name]
    _check_header(spec, path)
    lib_path = engine_library(spec, Path(path).parent)
    if lib_path is None:
        raise FileNotFoundError(
            f"{spec.name}: no engine build for this CPU next to {path}"
        )
    lib = ctypes.CDLL(str(lib_path))
    _declare(lib)
    if lib.noizzen_abi_version() != ENGINE_ABI:
        raise ValueError(
            f"{spec.name}: engine ABI {lib.noizzen_abi_version()} != {ENGINE_ABI}"
        )
    if lib.noizzen_spec_size() != (spec.win_len // 2 + 1) * 2:
        raise ValueError(f"{spec.name}: {lib_path.name} was built for another model")
    # The engine re-checks the size and the checksum it was built with: a truncated,
    # corrupt or other model's file is refused instead of producing noise.
    handle = lib.noizzen_model_open(path.encode(), 1)
    if not handle:
        raise ValueError(
            f"{spec.name}: weights file rejected by {lib_path.name}: {path}"
        )
    # Which CPU build runs here: the x86-64-v2 fallback is ~60% slower than v3.
    logger.info(f"Noizzen engine loaded: {lib_path.name}")
    return LoadedModel(
        spec=spec, window=vorbis_window(spec.win_len), lib=lib, handle=handle
    )
