"""Noise-suppression models: the registry and the once-per-process ONNX loader.

Every model is a causal ONNX graph: one STFT frame (real/imag) plus the
recurrent state in, the enhanced frame plus the next state out. Sources and
licence: docs/NOISE_FILTER.md.
"""

import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str  # in BREEZE_BUDDY_NOISE_MODEL_DIR (provisioned by Docker)
    sample_rate: int
    win_len: int
    hop: int
    delay_hops: int  # look-ahead: the output frame lags the input by this many hops


# Ceva DPDFNet; both look 4 hops (40 ms) ahead.
MODELS = {
    "dpdfnet2_8k": ModelSpec("dpdfnet2_8k", "dpdfnet2_8khz.onnx", 8000, 160, 80, 4),
    "dpdfnet_base_16k": ModelSpec(
        "dpdfnet_base_16k", "dpdfnet_baseline_16khz.onnx", 16000, 320, 160, 4
    ),
}


@dataclass(frozen=True)
class LoadedModel:
    """A model's read-only runtime, shared by every call in the process."""

    spec: ModelSpec
    session: ort.InferenceSession
    spec_input: str
    spec_shape: tuple
    state_inputs: tuple  # state input names, in session order
    init_states: tuple
    window: np.ndarray


def vorbis_window(n: int) -> np.ndarray:
    """DPDFNet's analysis/synthesis window (power-complementary at 50% overlap)."""
    s = np.sin(0.5 * np.pi * (np.arange(n) + 0.5) / (n / 2))
    return np.sin(0.5 * np.pi * s * s).astype(np.float32)


def find_model_file(name: str, model_dir: Path) -> Optional[Path]:
    path = model_dir / MODELS[name].file
    return path if path.is_file() else None


def _initial_states(session: ort.InferenceSession) -> tuple:
    """Zero state, seeded with the normaliser values DPDFNet keeps in its metadata."""
    meta = session.get_modelmeta().custom_metadata_map
    states = []
    for inp in session.get_inputs()[1:]:
        state = np.zeros(inp.shape, dtype=np.float32)
        if "erb_norm_init" in meta and state.ndim == 1:
            erb_n = int(meta["erb_norm_state_size"])
            spec_n = int(meta["spec_norm_state_size"])
            state[:erb_n] = [float(v) for v in meta["erb_norm_init"].split(",")]
            state[erb_n : erb_n + spec_n] = [
                float(v) for v in meta["spec_norm_init"].split(",")
            ]
        states.append(state)
    return tuple(states)


def _check_metadata(spec: ModelSpec, session: ort.InferenceSession) -> None:
    meta = session.get_modelmeta().custom_metadata_map
    expected = {
        "sample_rate": spec.sample_rate,
        "window_length": spec.win_len,
        "hop_length": spec.hop,
    }
    for key, want in expected.items():
        if key in meta and int(meta[key]) != want:
            raise ValueError(f"{spec.name}: model {key}={meta[key]}, spec says {want}")


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


@lru_cache(maxsize=None)
def _load(name: str, path: str) -> LoadedModel:
    spec = MODELS[name]
    options = ort.SessionOptions()
    # One thread per run: concurrency comes from many calls, not one frame.
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    # Load test: 8 threads carried 26-28 calls with the shared arena, 36-51 without.
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(
        path, sess_options=options, providers=["CPUExecutionProvider"]
    )
    _check_metadata(spec, session)
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != len(outputs) or len(inputs) < 2:
        raise ValueError(f"{name}: expected (spec, *state) -> (spec, *state)")
    bins = spec.win_len // 2 + 1
    spec_shape = tuple(inputs[0].shape)
    if int(np.prod(spec_shape)) != bins * 2:
        raise ValueError(f"{name}: spec input {spec_shape} != {bins} bins x 2")
    for i, o in zip(inputs[1:], outputs[1:]):
        if list(i.shape) != list(o.shape):
            raise ValueError(f"{name}: state {i.name}{i.shape} != {o.name}{o.shape}")
    return LoadedModel(
        spec=spec,
        session=session,
        spec_input=inputs[0].name,
        spec_shape=spec_shape,
        state_inputs=tuple(i.name for i in inputs[1:]),
        init_states=_initial_states(session),
        window=vorbis_window(spec.win_len),
    )
