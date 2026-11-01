"""The noise filter engine computes what ONNX Runtime computes, and refuses what it wasn't built for.

Pins: a synthetic model using every op the builder supports matches ONNX Runtime frame by
frame, state included; the real DPDFNet models match when provisioned locally; corrupt,
truncated, mismatched or foreign weights are refused; frames of the wrong shape are refused;
streams are independent across threads; the loader only picks builds this CPU can run.

The engine is built the way the Dockerfile builds it: build.py in a throwaway environment
with onnx (`uv run --no-project --with onnx ...`), then compiled with the system compiler.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import onnxruntime as ort
import pytest

from app.ai.voice.noise_filter.noizzen import NoizzenFilter, models
from app.ai.voice.noise_filter.noizzen.filter import StreamEnhancer

BUILD = Path(models.__file__).parent / "engine" / "build.py"
UV = shutil.which("uv")
CC = shutil.which(os.environ.get("CC", "cc"))
pytestmark = pytest.mark.skipif(not (UV and CC), reason="needs uv and a C compiler")


def _build_env(*args: str) -> None:
    """Python with onnx in a throwaway environment, exactly as the Dockerfile runs build.py."""
    assert UV
    cmd = [
        UV,
        "run",
        "--no-project",
        "--with",
        "onnx==1.19.1",
        "--with",
        "numpy==2.2.6",
        "python",
        *args,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


# Every op build.py supports, with the shapes that exercise each kernel path: broadcasting,
# a reversed slice, 1x1 convs on both sides of the gemm cut-over (81 and 41 positions), a
# strided depthwise conv, a 3x3 conv with rows, batched MatMul, a bidirectional GRU whose
# initial state comes from the recurrent state, reflect padding and an unused Split output.
SYNTHETIC = textwrap.dedent("""
    import sys
    import numpy as np
    import onnx
    from onnx import TensorProto, helper as h, numpy_helper as nh

    rng = np.random.default_rng(7)
    inits, nodes = [], []

    def w(name, shape, scale=0.3):
        inits.append(nh.from_array((rng.standard_normal(shape) * scale).astype(np.float32), name))
        return name

    def c(name, value, dtype=np.float32):
        inits.append(nh.from_array(np.array(value, dtype=dtype), name))
        return name

    def n(op, ins, outs, **kw):
        nodes.append(h.make_node(op, ins, outs, name=f"{op}_{len(nodes)}", **kw))
        return outs[0] if len(outs) == 1 else outs

    i64 = np.int64
    spec = n("Identity", ["spec"], ["spec_id"])
    sq = n("Pow", [spec, c("two", 2.0)], ["sq"])
    pw = n("ReduceSum", [sq, c("ax_last", [-1], i64)], ["pw"], keepdims=0)
    mag = n("Sqrt", [n("Add", [pw, c("eps", 1e-6)], ["pw_eps"])], ["mag"])
    lmag = n("Log", [n("Add", [mag, c("one", 1.0)], ["mag1"])], ["lmag"])
    re = n("Gather", ["spec", c("i0", 0, i64)], ["re"], axis=3)
    im = n("Gather", ["spec", c("i1", 1, i64)], ["im"], axis=3)
    ch4 = n("Unsqueeze", [n("Concat", [re, im], ["ch"], axis=1), c("ax2", [2], i64)], ["ch4"])
    r1 = n("Relu", [n("Conv", [ch4, w("wc1", [8, 2, 1, 3]), w("bc1", [8])], ["c1"], pads=[0, 1, 0, 1])], ["r1"])
    p1 = n("Conv", [r1, w("wp1", [8, 8, 1, 1]), w("bp1", [8])], ["p1"])
    dw = n("Conv", [p1, w("wdw", [8, 1, 1, 3])], ["dw"], group=8, strides=[1, 2], pads=[0, 1, 0, 1])
    p2 = n("Conv", [dw, w("wp2", [8, 8, 1, 1]), w("bp2", [8])], ["p2"])
    tr = n("Transpose", [n("Squeeze", [p2, c("ax2b", [2], i64)], ["s2"])], ["tr"], perm=[0, 2, 1])
    ln = n("LayerNormalization", [n("MatMul", [tr, w("wm", [8, 16])], ["mm"]), w("lg", [16]), w("lb", [16])], ["ln"], axis=-1, epsilon=1e-5)
    gate = n("Mul", [n("Sigmoid", [ln], ["sg"]), n("Tanh", [ln], ["th"])], ["gate"])
    x = n("Transpose", [gate], ["X"], perm=[1, 0, 2])
    st_a = n("Slice", ["state_in", c("s0", [0], i64), c("e24", [24], i64)], ["st_a"])
    st_h = n("Slice", ["state_in", c("s24", [24], i64), c("e40", [40], i64), c("ax0", [0], i64), c("st1", [1], i64)], ["st_h"])
    h0 = n("Reshape", [st_h, c("sh_h0", [2, 1, 8], i64)], ["h0"])
    y, yh = n("GRU", [x, w("wg", [2, 24, 16]), w("rg", [2, 24, 8]), w("bg", [2, 48]), "", h0], ["Y", "Yh"],
              direction="bidirectional", hidden_size=8, linear_before_reset=1)
    gm = n("Gemm", [n("Reshape", [y, c("sh_y", [41, 16], i64)], ["Yr"]), w("wgm", [12, 16]), w("bgm", [12])], ["gm"], transB=1)
    bm = n("Reshape", [n("MatMul", [dw, w("wb", [8, 41, 4])], ["bm"]), c("sh_b", [1, 32], i64)], ["bm2"])
    c3in = n("Reshape", [st_a, c("sh_c3", [1, 1, 3, 8], i64)], ["c3in"])
    c3 = n("Flatten", [n("Conv", [c3in, w("wc3", [4, 1, 3, 3])], ["c3"], pads=[0, 1, 0, 1])], ["c3f"], axis=1)
    feat = n("Concat", [n("Reshape", [gm, c("sh_g", [1, 492], i64)], ["g2"]), bm, c3], ["feat"], axis=1)
    o4 = n("Reshape", [n("MatMul", [feat, w("wo", [556, 160], 0.05)], ["o"]), c("sh_o", [1, 1, 80, 2], i64)], ["o4"])
    pad = n("Pad", [o4, c("pads", [0, 0, 0, 0, 0, 0, 1, 0], i64)], ["pad"], mode="reflect")
    gain = n("Unsqueeze", [n("Sigmoid", [lmag], ["sgl"]), c("ax3", [3], i64)], ["gain"])
    a1 = n("Add", [n("Mul", ["spec", gain], ["m1"]), n("Div", [pad, c("ten", 10.0)], ["d1"])], ["a1"])
    n("Sub", [a1, n("Mul", ["spec", c("small", 0.1)], ["m2"])], ["spec_e"])
    sa, sb, sc = n("Split", ["state_in", c("split", [24, 16, 8], i64)], ["sa", "sb", "sc"], axis=0)
    c3v = n("Reshape", [c3, c("sh_c3v", [32], i64)], ["c3v"])
    rev = n("Slice", [c3v, c("s23", [23], i64), c("em33", [-33], i64), c("ax0b", [0], i64), c("stm1", [-1], i64)], ["rev"])
    na = n("Tanh", [n("Add", [sa, rev], ["na0"])], ["na"])
    nb = n("Reshape", [yh, c("sh_yh", [16], i64)], ["nb"])
    nc = n("Relu", [n("Sub", [sc, c("half", 0.5)], ["nc0"])], ["nc"])
    n("Concat", [na, nb, nc], ["state_out"], axis=0)

    graph = h.make_graph(
        nodes, "synthetic",
        [h.make_tensor_value_info("spec", TensorProto.FLOAT, [1, 1, 81, 2]),
         h.make_tensor_value_info("state_in", TensorProto.FLOAT, [48])],
        [h.make_tensor_value_info("spec_e", TensorProto.FLOAT, [1, 1, 81, 2]),
         h.make_tensor_value_info("state_out", TensorProto.FLOAT, [48])],
        inits,
    )
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 17)])
    model.ir_version = 8
    for k, v in {"sample_rate": "8000", "window_length": "160", "hop_length": "80"}.items():
        model.metadata_props.add(key=k, value=v)
    onnx.checker.check_model(model, full_check=True)
    onnx.save(model, sys.argv[1])
    """)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory) -> Path:
    """A synthetic model named like DPDFNet-8k, so MODELS['dpdfnet2_8k'] loads its engine."""
    d = tmp_path_factory.mktemp("engine")
    (d / "make.py").write_text(SYNTHETIC)
    _build_env(str(d / "make.py"), str(d / "dpdfnet2_8khz.onnx"))
    _build_env(str(BUILD), str(d), "--meta")
    return d


def _ort(path: Path) -> ort.InferenceSession:
    o = ort.SessionOptions()
    o.intra_op_num_threads = 1
    o.inter_op_num_threads = 1
    return ort.InferenceSession(
        str(path), sess_options=o, providers=["CPUExecutionProvider"]
    )


def _frames(n: int, bins: int, seed: int) -> list:
    rng = np.random.default_rng(seed)
    return [
        (rng.standard_normal((bins, 2)) * 0.05).astype(np.float32) for _ in range(n)
    ]


def _rel(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).max() / (np.abs(b).max() + 1e-12))


def _ref_step(
    ref: ort.InferenceSession, ri: np.ndarray, state: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """ONNX Runtime on one frame: (enhanced spectrum, new state)."""
    spec_e, state_out = ref.run(
        None, {"spec": ri.reshape(1, 1, *ri.shape), "state_in": state}
    )
    return np.asarray(spec_e, np.float32), np.asarray(state_out, np.float32)


def test_synthetic_model_covers_every_supported_op(synthetic):
    ops = {
        v[3]
        for v in json.loads((synthetic / "dpdfnet2_8khz.meta.json").read_text())[
            "dbg"
        ].values()
    }
    supported = {
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Pow",
        "Relu",
        "Sigmoid",
        "Tanh",
        "Sqrt",
        "Log",
        "ReduceSum",
        "LayerNormalization",
        "Gemm",
        "MatMul",
        "Conv",
        "GRU",
        "Slice",
        "Split",
        "Concat",
        "Transpose",
        "Gather",
        "Pad",
        "Reshape",
        "Squeeze",
        "Unsqueeze",
        "Flatten",
        "Identity",
    }
    assert supported <= ops


def test_engine_matches_onnx_runtime_frame_by_frame(synthetic):
    """Same input and state every frame: output and new state match ONNX Runtime."""
    model = models.load_model("dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights"))
    ref = _ort(synthetic / "dpdfnet2_8khz.onnx")
    stream = model.open_stream()
    state = np.zeros(48, np.float32)
    for ri in _frames(40, 81, 1):
        want_spec, want_state = _ref_step(ref, ri, state)
        got_spec, got_state = np.empty(162, np.float32), np.empty(48, np.float32)
        model.lib.noizzen_stream_step_state(
            stream._handle, ri.reshape(-1), state, got_spec, got_state, None
        )
        assert _rel(got_spec, want_spec.reshape(-1)) < 1e-4
        assert _rel(got_state, want_state) < 1e-4
        state = want_state


def test_engine_stream_carries_its_own_state(synthetic):
    """A stream fed frame after frame tracks ONNX Runtime fed its own state back."""
    model = models.load_model("dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights"))
    ref = _ort(synthetic / "dpdfnet2_8khz.onnx")
    stream, state = model.open_stream(), np.zeros(48, np.float32)
    for ri in _frames(200, 81, 2):
        want, state = _ref_step(ref, ri, state)
        assert _rel(stream.step(ri), want.reshape(81, 2)) < 1e-4


def test_streams_are_independent_across_threads(synthetic):
    model = models.load_model("dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights"))
    inputs = [_frames(300, 81, s) for s in (3, 4, 5, 6)]
    alone = []
    for frames in inputs:
        s = model.open_stream()
        alone.append([s.step(f) for f in frames])
    together: list = [None] * len(inputs)

    def run(i):
        s = model.open_stream()
        together[i] = [s.step(f) for f in inputs[i]]

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(inputs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for a, b in zip(alone, together):
        assert all(np.array_equal(x, y) for x, y in zip(a, b))


def test_engine_refuses_a_frame_of_the_wrong_shape(synthetic):
    stream = models.load_model(
        "dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights")
    ).open_stream()
    with pytest.raises(ValueError):
        stream.step(np.zeros((80, 2), np.float32))
    with pytest.raises(ValueError):
        stream.step(np.zeros((81, 2), np.float64))


@pytest.mark.parametrize(
    "damage", ["flip_payload", "truncate", "other_rate", "not_weights"]
)
def test_damaged_or_foreign_weights_are_refused(synthetic, tmp_path, damage):
    for f in synthetic.glob("dpdfnet2_8khz.*"):
        shutil.copy2(f, tmp_path / f.name)
    path = tmp_path / "dpdfnet2_8khz.weights"
    data = bytearray(path.read_bytes())
    if damage == "flip_payload":
        data[200] ^= 0x01
    elif damage == "truncate":
        data = data[:-4]
    elif damage == "other_rate":
        data[28:32] = (16000).to_bytes(4, "little")
    else:
        data[:8] = b"NOTNOIZZ"
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        models.load_model("dpdfnet2_8k", str(path))


def test_another_models_weights_are_refused(synthetic):
    """The 16 kHz spec must not run on 8 kHz weights."""
    with pytest.raises(ValueError):
        models.load_model("dpdfnet_base_16k", str(synthetic / "dpdfnet2_8khz.weights"))


def test_weights_without_an_engine_for_this_cpu_are_not_provisioned(
    synthetic, tmp_path
):
    shutil.copy2(
        synthetic / "dpdfnet2_8khz.weights", tmp_path / "dpdfnet2_8khz.weights"
    )
    assert models.find_model_file("dpdfnet2_8k", tmp_path) is None
    assert (
        models.find_model_file("dpdfnet2_8k", synthetic)
        == synthetic / "dpdfnet2_8khz.weights"
    )


@pytest.mark.parametrize(
    "machine, flags, expected",
    [
        ("x86_64", models._X86_64_V3 | {"sse4_2"}, ("x86-64-v3", "x86-64-v2")),
        ("x86_64", (models._X86_64_V3 - {"avx2"}) | {"sse4_2"}, ("x86-64-v2",)),
        ("aarch64", set(), ("arm64",)),
        ("riscv64", set(), ()),
    ],
)
def test_the_loader_only_picks_builds_this_cpu_runs(
    monkeypatch, machine, flags, expected
):
    monkeypatch.setattr(models.platform, "machine", lambda: machine)
    monkeypatch.setattr(models, "_cpu_flags", lambda: flags)
    models.cpu_variants.cache_clear()
    try:
        assert models.cpu_variants() == expected
    finally:
        models.cpu_variants.cache_clear()


def test_the_build_that_loads_is_logged(synthetic, monkeypatch):
    """The x86-64-v2 fallback is much slower, so a node running it must be visible."""
    lines: list = []
    monkeypatch.setattr(models, "logger", SimpleNamespace(info=lines.append))
    models._load.cache_clear()
    models.load_model("dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights"))
    best = models.cpu_variants()[0]
    assert lines == [f"Noizzen engine loaded: dpdfnet2_8khz.{best}.so"]


async def test_noizzen_filter_runs_on_the_engine(synthetic):
    """pipecat's contract end to end: int16 bytes in 20 ms pieces, finite audio back."""
    f = NoizzenFilter(
        model="dpdfnet2_8k", model_path=synthetic / "dpdfnet2_8khz.weights"
    )
    await f.start(8000)
    assert f._enhancer is not None
    rng = np.random.default_rng(9)
    pcm = (rng.standard_normal(8000) * 2000).astype(np.int16).tobytes()
    out = [await f.filter(pcm[i : i + 320]) for i in range(0, len(pcm), 320)]
    assert f._enhancer is not None  # never fell back to passthrough
    assert sum(len(o) for o in out) == len(pcm) - 160  # the 10 ms framing hold
    await f.stop()


def _initial_state(sess: ort.InferenceSession) -> np.ndarray:
    meta = sess.get_modelmeta().custom_metadata_map
    state = np.zeros(sess.get_inputs()[1].shape, np.float32)
    erb_n, spec_n = int(meta["erb_norm_state_size"]), int(meta["spec_norm_state_size"])
    state[:erb_n] = [float(v) for v in meta["erb_norm_init"].split(",")]
    state[erb_n : erb_n + spec_n] = [
        float(v) for v in meta["spec_norm_init"].split(",")
    ]
    return state


@pytest.mark.parametrize("name", ["dpdfnet2_8k", "dpdfnet_base_16k"])
def test_real_dpdfnet_engine_matches_onnx_runtime(tmp_path, name):
    """Runs where the DPDFNet ONNX files are provisioned (BREEZE_BUDDY_NOISE_MODEL_DIR)."""
    from app.core.config import static

    spec = models.MODELS[name]
    src = Path(static.BREEZE_BUDDY_NOISE_MODEL_DIR) / f"{spec.stem}.onnx"
    if not src.is_file():
        pytest.skip(f"{src} not provisioned")
    shutil.copy2(src, tmp_path / src.name)
    _build_env(str(BUILD), str(tmp_path))
    model = models.load_model(name, str(tmp_path / spec.file))
    ref = _ort(src)
    state, stream = _initial_state(ref), model.open_stream()
    bins = spec.win_len // 2 + 1
    want, got = [], []
    for ri in _frames(600, bins, 11):
        y, state = _ref_step(ref, ri, state)
        want.append(y.reshape(-1))
        got.append(stream.step(ri).reshape(-1))
    a, b = np.concatenate(want), np.concatenate(got)
    match_db = 10 * np.log10(np.sum(a**2) / np.sum((a - b) ** 2))
    assert match_db > 90, match_db


def test_stream_enhancer_uses_one_engine_stream_per_call(synthetic):
    model = models.load_model("dpdfnet2_8k", str(synthetic / "dpdfnet2_8khz.weights"))
    a, b = StreamEnhancer(model, 8000), StreamEnhancer(model, 8000)
    assert a._engine is not b._engine
